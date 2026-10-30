from datetime import datetime, timedelta, timezone
import logging
import threading

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .autograb import auto_grab
from .config import settings
from .database import SessionLocal
from .models import Opportunity, ScanRun
from .scoring import calculate_score
from .telegram import notify_opportunities
from .torznab import TorznabClient, TorznabItem

logger = logging.getLogger(__name__)
_scan_lock = threading.Lock()


def _accepted(item: TorznabItem) -> bool:
    return (
        item.leechers >= settings.min_leechers
        and item.seeders <= settings.max_seeders
        and (item.freeleech or not settings.freeleech_only)
    )


def _fields(item: TorznabItem, now: datetime) -> dict:
    return {
        "title": item.title,
        "download_url": item.download_url,
        "category": item.category,
        "size_bytes": item.size_bytes,
        "seeders": item.seeders,
        "leechers": item.leechers,
        "grabs": item.grabs,
        "freeleech": item.freeleech,
        "download_factor": item.download_factor,
        "upload_factor": item.upload_factor,
        "score": calculate_score(
            seeders=item.seeders,
            leechers=item.leechers,
            freeleech=item.freeleech,
            published_at=item.published_at,
            size_bytes=item.size_bytes,
        ),
        "published_at": item.published_at,
        "last_seen_at": now,
        "updated_at": now,
    }


def _store_items(db: Session, items: list[TorznabItem], now: datetime, counters: dict) -> None:
    # Un flux multi-indexeurs peut répéter un guid : cet index garde aussi les
    # ajouts du scan en cours pour éviter une violation d'unicité au commit.
    known = {
        opportunity.guid: opportunity
        for opportunity in db.scalars(
            select(Opportunity).where(Opportunity.guid.in_({item.guid for item in items}))
        )
    }

    for item in items:
        if not _accepted(item):
            counters["rejected"] += 1
            continue

        fields = _fields(item, now)
        existing = known.get(item.guid)
        if existing:
            for name, value in fields.items():
                setattr(existing, name, value)
            counters["updated"] += 1
        else:
            known[item.guid] = Opportunity(guid=item.guid, first_seen_at=now, **fields)
            db.add(known[item.guid])
            counters["created"] += 1


def _prune(db: Session, now: datetime, counters: dict) -> None:
    # Le flush doit précéder le DELETE, sinon les torrents revus à ce scan
    # sont encore périmés en base. La session ne contient plus rien à
    # synchroniser : le commit qui suit recharge tout.
    db.flush()
    stale_before = now - timedelta(minutes=settings.stale_after_minutes)
    stale_result = db.execute(
        delete(Opportunity)
        .where(Opportunity.last_seen_at < stale_before)
        .execution_options(synchronize_session=False)
    )
    counters["stale_removed"] = stale_result.rowcount or 0

    history_before = now - timedelta(days=settings.scan_history_days)
    db.execute(
        delete(ScanRun)
        .where(ScanRun.started_at < history_before)
        .execution_options(synchronize_session=False)
    )


def scan_once() -> dict:
    if not _scan_lock.acquire(blocking=False):
        logger.warning("Scan ignoré : un autre scan est déjà en cours")
        return {"status": "skipped", "reason": "already_running"}

    now = datetime.now(timezone.utc)
    db = SessionLocal()
    run_id = None
    counters = {"received": 0, "created": 0, "updated": 0, "rejected": 0, "stale_removed": 0}

    try:
        run = ScanRun(started_at=now)
        db.add(run)
        db.commit()
        run_id = run.id

        items = TorznabClient().search()
        counters["received"] = len(items)
        _store_items(db, items, now, counters)
        _prune(db, now, counters)

        run.finished_at = datetime.now(timezone.utc)
        run.status = "success"
        for key, value in counters.items():
            setattr(run, key, value)

        db.commit()
        logger.info(
            "Scan terminé | reçus=%d | créés=%d | mis à jour=%d | rejetés=%d | périmés=%d",
            counters["received"], counters["created"], counters["updated"],
            counters["rejected"], counters["stale_removed"],
        )
        # Dans cet ordre : l'alerte indique si le torrent vient d'être envoyé.
        grabbed = auto_grab(now)
        notified = notify_opportunities(now)
        return {"status": "success", "grabbed": grabbed, "notified": notified, **counters}

    except Exception as exc:
        db.rollback()
        failed = db.get(ScanRun, run_id) if run_id else None
        if failed:
            failed.finished_at = datetime.now(timezone.utc)
            failed.status = "error"
            failed.error = str(exc)
            db.commit()
        logger.exception("Scan en erreur")
        return {"status": "error", "error": str(exc)}
    finally:
        db.close()
        _scan_lock.release()
