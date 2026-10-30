from datetime import datetime, timedelta, timezone
from html import escape
import logging
import re
import threading

import requests
from sqlalchemy import delete, desc, select

from .autograb import DownloadBlocked, send_opportunity
from .config import settings
from .database import SessionLocal
from .models import Grab, Notification, Opportunity
from .qbittorrent import QBittorrentError, get_qbittorrent

logger = logging.getLogger(__name__)
GIB = 1024 ** 3
POLL_SECONDS = 25
RETRY_SECONDS = 30
ANSWER_MAX_LENGTH = 200
CALLBACK_DATA = re.compile(r"grab:(\d+)")


class TelegramError(RuntimeError):
    pass


def configured() -> bool:
    return bool(settings.telegram_bot_token and settings.telegram_chat_id)


def _call(method: str, payload: dict, timeout: int | None = None) -> requests.Response:
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}",
            json=payload,
            timeout=timeout or settings.request_timeout_seconds,
        )
    except requests.RequestException as exc:
        # Le message de requests contient l'URL, donc le token du bot.
        raise TelegramError(f"Telegram injoignable : {type(exc).__name__}") from None

    if response.status_code != 200:
        raise TelegramError(f"Telegram a refusé la requête : HTTP {response.status_code}")
    return response


def send_message(text: str, reply_markup: dict | None = None) -> None:
    payload = {
        "chat_id": settings.telegram_chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    _call("sendMessage", payload)


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


def _download_button(notification_id: int) -> dict:
    # L'id de notification plutôt que celui de l'opportunité : SQLite peut
    # réattribuer ce dernier, et un ancien bouton lancerait un autre torrent.
    return {"inline_keyboard": [[{"text": "⬇️ Télécharger", "callback_data": f"grab:{notification_id}"}]]}


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
    if not configured():
        return 0

    sent = 0
    try:
        opportunities, grabbed = _pending(seen_at)
        with SessionLocal() as db:
            for opportunity in opportunities:
                already_grabbed = opportunity.guid in grabbed
                # Commit avant l'envoi : le bouton a besoin de l'id, et la base
                # ne doit pas rester verrouillée pendant l'appel réseau.
                notification = Notification(guid=opportunity.guid)
                db.add(notification)
                db.commit()

                offer_button = opportunity.download_url and not already_grabbed
                button = _download_button(notification.id) if offer_button else None
                try:
                    send_message(_message(opportunity, already_grabbed), button)
                except TelegramError:
                    db.delete(notification)
                    db.commit()
                    raise
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


def _answer(callback: dict, text: str, alert: bool = False) -> None:
    _call(
        "answerCallbackQuery",
        {"callback_query_id": callback["id"], "text": text[:ANSWER_MAX_LENGTH], "show_alert": alert},
    )


def _remove_button(message: dict) -> None:
    _call(
        "editMessageReplyMarkup",
        {"chat_id": message["chat"]["id"], "message_id": message["message_id"]},
    )


def handle_callback(callback: dict) -> None:
    message = callback.get("message") or {}
    if str((message.get("chat") or {}).get("id")) != settings.telegram_chat_id:
        logger.warning("Clic Telegram refusé : conversation non autorisée")
        _answer(callback, "Non autorisé", alert=True)
        return

    match = CALLBACK_DATA.fullmatch(callback.get("data") or "")
    if match is None:
        _answer(callback, "Action inconnue")
        return

    with SessionLocal() as db:
        notification = db.get(Notification, int(match.group(1)))
        guid = notification.guid if notification else None
        opportunity = db.scalar(select(Opportunity).where(Opportunity.guid == guid)) if guid else None
        already_sent = guid and db.scalar(
            select(Grab.id).where(Grab.guid == guid, Grab.status == "sent")
        )

    if already_sent:
        _answer(callback, "Déjà envoyé à qBittorrent")
        _remove_button(message)
        return
    if opportunity is None or not opportunity.download_url:
        _answer(callback, "Ce torrent n'est plus disponible", alert=True)
        _remove_button(message)
        return

    try:
        send_opportunity(opportunity, get_qbittorrent(), source="telegram")
    except (DownloadBlocked, QBittorrentError) as exc:
        _answer(callback, str(exc), alert=True)
        return

    _answer(callback, "Envoyé à qBittorrent ✅")
    _remove_button(message)


def poll_updates(offset: int | None) -> int | None:
    response = _call(
        "getUpdates",
        {"offset": offset, "timeout": POLL_SECONDS, "allowed_updates": ["callback_query"]},
        timeout=POLL_SECONDS + 10,
    )
    for update in response.json().get("result", []):
        # Avancé avant le traitement : un clic qui échoue ne doit pas être rejoué
        # en boucle, ni relancer un téléchargement.
        offset = update["update_id"] + 1
        if "callback_query" not in update:
            continue
        try:
            handle_callback(update["callback_query"])
        except Exception:
            logger.exception("Clic Telegram en erreur")
    return offset


def run_bot(stop: threading.Event) -> None:
    """Écoute les clics par long polling : aucun port n'a besoin d'être ouvert."""
    offset = None
    while not stop.is_set():
        try:
            offset = poll_updates(offset)
        except TelegramError as exc:
            logger.warning("Écoute Telegram interrompue : %s", exc)
            stop.wait(RETRY_SECONDS)
        except Exception:
            logger.exception("Écoute Telegram en erreur")
            stop.wait(RETRY_SECONDS)
