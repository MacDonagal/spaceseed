from datetime import datetime, timedelta
from html import escape
import logging
import re
import threading

import requests
from sqlalchemy import delete, desc, select

from . import c411
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
TITLE_MAX_LENGTH = 150
BUTTON_TITLE_LENGTH = 40
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


def _ratio_recap() -> str | None:
    account = c411.summary()
    if account is None:
        return None
    if "error" in account:
        return f"⚠️ C411 : vérification impossible ({escape(account['error'])})"
    if not account["allowed"]:
        return f"⛔ C411 : téléchargement bloqué ({escape(account['blocked_reason'] or 'raison inconnue')})"

    warning = "⚠️ " if account["below_reserve"] else ""
    return (
        f"{warning}📊 Ratio {account['ratio']} · Marge {account['margin_gb']} Go\n"
        f"⬆️ {account['uploaded_gb']} Go · ⬇️ {account['downloaded_gb']} Go"
    )


def _shorten(title: str, length: int) -> str:
    return title if len(title) <= length else f"{title[:length - 1]}…"


def _digest_item(position: int, opportunity: Opportunity, grabbed: bool) -> str:
    details = [
        f"Score {opportunity.score:g}",
        f"S/L {opportunity.seeders}/{opportunity.leechers}",
        f"{opportunity.size_bytes / GIB:.1f} Go",
    ]
    if opportunity.freeleech:
        details.append("FreeLeech")

    lines = [f"<b>{position}.</b> {escape(_shorten(opportunity.title, TITLE_MAX_LENGTH))}", " · ".join(details)]
    if grabbed:
        lines.append("✅ Envoyé automatiquement à qBittorrent")
    return "\n".join(lines)


def _digest(opportunities: list[Opportunity], grabbed: set[str]) -> str:
    parts = ["🚀 <b>Nouvelles opportunités</b>"]
    recap = _ratio_recap()
    if recap:
        parts.append(recap)
    parts += [
        _digest_item(position, opportunity, opportunity.guid in grabbed)
        for position, opportunity in enumerate(opportunities, start=1)
    ]
    return "\n\n".join(parts)


def _download_buttons(
    opportunities: list[Opportunity], notifications: list[Notification], grabbed: set[str]
) -> dict | None:
    # L'id de notification plutôt que celui de l'opportunité : SQLite peut
    # réattribuer ce dernier, et un ancien bouton lancerait un autre torrent.
    rows = [
        [{
            "text": f"⬇️ {position}. {_shorten(opportunity.title, BUTTON_TITLE_LENGTH)}",
            "callback_data": f"grab:{notification.id}",
        }]
        for position, (opportunity, notification) in enumerate(zip(opportunities, notifications), start=1)
        if opportunity.download_url and opportunity.guid not in grabbed
    ]
    return {"inline_keyboard": rows} if rows else None


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


def _sent_recently(seen_at: datetime) -> bool:
    if settings.telegram_min_interval_minutes <= 0:
        return False
    # Lu en base pour que la limite tienne après un redémarrage.
    window_start = seen_at - timedelta(minutes=settings.telegram_min_interval_minutes)
    with SessionLocal() as db:
        return db.scalar(select(Notification.id).where(Notification.sent_at > window_start).limit(1)) is not None


def _purge(seen_at: datetime) -> None:
    history_before = seen_at - timedelta(days=settings.scan_history_days)
    with SessionLocal() as db:
        db.execute(delete(Notification).where(Notification.sent_at < history_before))
        db.commit()


def notify_opportunities(seen_at: datetime) -> int:
    """Envoie une alerte groupée, au plus une par intervalle ; n'interrompt jamais le scan."""
    if not configured():
        return 0

    try:
        _purge(seen_at)
        if _sent_recently(seen_at):
            return 0

        opportunities, grabbed = _pending(seen_at)
        if not opportunities:
            return 0

        with SessionLocal() as db:
            # Commit avant l'envoi : les boutons ont besoin des id, et la base
            # ne doit pas rester verrouillée pendant l'appel réseau.
            notifications = [Notification(guid=item.guid, sent_at=seen_at) for item in opportunities]
            db.add_all(notifications)
            db.commit()

            try:
                send_message(
                    _digest(opportunities, grabbed),
                    _download_buttons(opportunities, notifications, grabbed),
                )
            except TelegramError:
                for notification in notifications:
                    db.delete(notification)
                db.commit()
                raise
        return len(opportunities)
    except TelegramError as exc:
        # Les torrents non signalés restent éligibles au prochain scan.
        logger.warning("Notification Telegram interrompue : %s", exc)
    except Exception:
        logger.exception("Notification Telegram en erreur")

    return 0


def _answer(callback: dict, text: str, alert: bool = False) -> None:
    _call(
        "answerCallbackQuery",
        {"callback_query_id": callback["id"], "text": text[:ANSWER_MAX_LENGTH], "show_alert": alert},
    )


def _remove_button(message: dict, data: str) -> None:
    # Une alerte groupée garde les boutons des autres torrents.
    keyboard = (message.get("reply_markup") or {}).get("inline_keyboard", [])
    remaining = [
        [button for button in row if button.get("callback_data") != data]
        for row in keyboard
    ]
    _call(
        "editMessageReplyMarkup",
        {
            "chat_id": message["chat"]["id"],
            "message_id": message["message_id"],
            "reply_markup": {"inline_keyboard": [row for row in remaining if row]},
        },
    )


def _authorized(message: dict) -> bool:
    return str((message.get("chat") or {}).get("id")) == settings.telegram_chat_id


def handle_callback(callback: dict) -> None:
    message = callback.get("message") or {}
    if not _authorized(message):
        logger.warning("Clic Telegram refusé : conversation non autorisée")
        _answer(callback, "Non autorisé", alert=True)
        return

    data = callback.get("data") or ""
    match = CALLBACK_DATA.fullmatch(data)
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
        _remove_button(message, data)
        return
    if opportunity is None or not opportunity.download_url:
        _answer(callback, "Ce torrent n'est plus disponible", alert=True)
        _remove_button(message, data)
        return

    try:
        send_opportunity(opportunity, get_qbittorrent(), source="telegram")
    except (DownloadBlocked, QBittorrentError) as exc:
        _answer(callback, str(exc), alert=True)
        return

    _answer(callback, "Envoyé à qBittorrent ✅")
    _remove_button(message, data)


def handle_message(message: dict) -> None:
    if not _authorized(message):
        return
    command = (message.get("text") or "").split("@")[0].strip().lower()
    if command == "/ratio":
        send_message(_ratio_recap() or "Le contrôle C411 n'est pas configuré.")


def poll_updates(offset: int | None) -> int | None:
    response = _call(
        "getUpdates",
        {"offset": offset, "timeout": POLL_SECONDS, "allowed_updates": ["callback_query", "message"]},
        timeout=POLL_SECONDS + 10,
    )
    for update in response.json().get("result", []):
        # Avancé avant le traitement : un clic qui échoue ne doit pas être rejoué
        # en boucle, ni relancer un téléchargement.
        offset = update["update_id"] + 1
        try:
            if "callback_query" in update:
                handle_callback(update["callback_query"])
            elif "message" in update:
                handle_message(update["message"])
        except Exception:
            logger.exception("Commande Telegram en erreur")
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
