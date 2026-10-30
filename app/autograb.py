from datetime import datetime, timezone
import logging

from sqlalchemy import desc, or_, select
from sqlalchemy.exc import SQLAlchemyError

from .c411 import download_block_reason
from .config import settings
from .database import SessionLocal
from .models import Grab, Opportunity
from .qbittorrent import QBittorrentError, get_qbittorrent

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 3
GIB = 1024 ** 3


def save_grab(opportunity: Opportunity, *, source: str, status: str, error: str | None = None) -> None:
    # Le torrent est déjà parti ou déjà refusé : une erreur ici ne doit pas
    # faire échouer l'envoi, au pire qBittorrent ignorera un doublon.
    try:
        with SessionLocal() as db:
            grab = db.scalar(select(Grab).where(Grab.guid == opportunity.guid))
            if grab is None:
                grab = Grab(guid=opportunity.guid, attempts=0)
                db.add(grab)
            grab.title = opportunity.title
            grab.size_bytes = opportunity.size_bytes
            grab.score = opportunity.score
            grab.source = source
            grab.status = status
            grab.error = error
            grab.attempts += 1
            grab.grabbed_at = datetime.now(timezone.utc)
            db.commit()
    except SQLAlchemyError:
        logger.exception("Envoi non enregistré | titre=%s", opportunity.title)


def _candidates(seen_at: datetime) -> list[Opportunity]:
    settled = select(Grab.guid).where(or_(Grab.status == "sent", Grab.attempts >= MAX_ATTEMPTS))
    query = (
        select(Opportunity)
        .where(
            Opportunity.last_seen_at >= seen_at,
            Opportunity.score >= settings.auto_grab_min_score,
            Opportunity.seeders >= settings.auto_grab_min_seeders,
            Opportunity.download_url.is_not(None),
            # Taille inconnue (0) exclue : l'espace disque ne serait pas vérifiable.
            Opportunity.size_bytes > 0,
            Opportunity.size_bytes <= settings.auto_grab_max_size_gb * GIB,
            Opportunity.guid.not_in(settled),
        )
        .order_by(desc(Opportunity.score))
    )
    if settings.auto_grab_freeleech_only:
        query = query.where(Opportunity.freeleech.is_(True))

    with SessionLocal() as db:
        return list(db.scalars(query))


def auto_grab(seen_at: datetime) -> int:
    """Envoie les meilleures opportunités du scan ; n'interrompt jamais le scan."""
    if not settings.auto_grab_enabled:
        return 0

    sent = 0
    try:
        service = get_qbittorrent()
        active = sum(1 for torrent in service.list_torrents() if torrent["progress"] < 1)
        slots = min(settings.auto_grab_max_per_scan, settings.auto_grab_max_active - active)
        if slots <= 0:
            logger.info("Envoi automatique suspendu | téléchargements en cours=%d", active)
            return 0

        free_bytes = service.free_space_bytes()
        reserve_bytes = settings.auto_grab_min_free_space_gb * GIB
        # Le profil C411 n'inclut pas encore ce qui vient de partir à ce scan.
        pending_bytes = 0

        for opportunity in _candidates(seen_at):
            if sent >= slots:
                break
            if free_bytes - opportunity.size_bytes < reserve_bytes:
                logger.info("Envoi automatique ignoré, espace insuffisant | titre=%s", opportunity.title)
                continue

            reason = download_block_reason(opportunity.size_bytes, opportunity.freeleech, pending_bytes)
            if reason:
                # Sans tentative comptée : le torrent redevient éligible si le ratio remonte.
                logger.warning("Envoi automatique bloqué | titre=%s | raison=%s", opportunity.title, reason)
                continue

            try:
                service.add_from_url(opportunity.download_url)
            except QBittorrentError as exc:
                save_grab(opportunity, source="auto", status="failed", error=str(exc))
                logger.warning("Envoi automatique refusé | titre=%s | erreur=%s", opportunity.title, exc)
                break

            save_grab(opportunity, source="auto", status="sent")
            logger.info("Envoi automatique | score=%.1f | titre=%s", opportunity.score, opportunity.title)
            free_bytes -= opportunity.size_bytes
            if not opportunity.freeleech:
                pending_bytes += opportunity.size_bytes
            sent += 1
    except Exception:
        logger.exception("Envoi automatique en erreur")

    return sent
