from datetime import datetime, timedelta, timezone
from html import escape
import logging

import requests
from sqlalchemy import delete, desc, select

from .config import settings
from .database import SessionLocal
from .models import Grab, Notification, Opportunity

logger = logging.getLogger(__name__)
GIB = 1024 ** 3


class TelegramError(RuntimeError):
    pass


def send_message(text: str) -> None:
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            json={
                "chat_id": settings.telegram_chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=settings.request_timeout_seconds,
        )
    except requests.RequestException as exc:
        # Le message de requests contient l'URL, donc le token du bot.
        raise TelegramError(f"Telegram injoignable : {type(exc).__name__}") from None

    if response.status_code != 200:
        raise TelegramError(f"Telegram a refusé le message : HTTP {response.status_code}")


def _message(opportunity: Opportunity, grabbed: bool) -> str:
    details = [
        f"Score {opportunity.score:g}",
        f"S/L {opportunity.seeders}/{opportunity.leechers}",
        f"{opportunity.size_bytes / GIB:.1f} Go",
    ]
    if opportunity.freeleech:
        details.append("FreeLeech")

    lines = ["🚀 <b>Nouvelle opportunité</b>", escape(opportunity.title), " · ".join(details)]
    if grabbed:
        lines.append("✅ Envoyé automatiquement à qBittorrent")
    return "\n".join(lines)


def _pending(seen_at: datetime) -> tuple[list[Opportunity], set[str]]:
    notified = select(Notification.guid)
    query = (
        select(Opportunity)
        .where(
            Opportunity.last_seen_at >= seen_at,
            Opportunity.score >= settings.telegram_min_score,
            Opportunity.guid.not_in(notified),
        )
        .order_by(desc(Opportunity.score))
        .limit(settings.telegram_max_per_scan)
    )
    if settings.telegram_freeleech_only:
        query = query.where(Opportunity.freeleech.is_(True))

    with SessionLocal() as db:
        opportunities = list(db.scalars(query))
        grabbed = set(db.scalars(select(Grab.guid).where(Grab.status == "sent")))
    return opportunities, grabbed


def notify_opportunities(seen_at: datetime) -> int:
    """Signale les nouvelles opportunités du scan ; n'interrompt jamais le scan."""
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        return 0

    sent = 0
    try:
        opportunities, grabbed = _pending(seen_at)
        with SessionLocal() as db:
            for opportunity in opportunities:
                send_message(_message(opportunity, opportunity.guid in grabbed))
                db.add(Notification(guid=opportunity.guid))
                db.commit()
                sent += 1

            history_before = seen_at - timedelta(days=settings.scan_history_days)
            db.execute(delete(Notification).where(Notification.sent_at < history_before))
            db.commit()
    except TelegramError as exc:
        # Les torrents non signalés restent éligibles au prochain scan.
        logger.warning("Notification Telegram interrompue : %s", exc)
    except Exception:
        logger.exception("Notification Telegram en erreur")

    return sent
