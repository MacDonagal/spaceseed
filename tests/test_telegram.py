from dataclasses import replace
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
import pytest
import requests
from sqlalchemy import func, select

from app import autograb, main, telegram
from app.database import SessionLocal
from app.models import Grab, Notification, Opportunity
from app.qbittorrent import QBittorrentError

GIB = 1024 ** 3
SEEN_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)


class FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code


@pytest.fixture
def bot(monkeypatch):
    configured = replace(
        telegram.settings, telegram_bot_token="123:secret", telegram_chat_id="42",
        telegram_min_score=100, telegram_freeleech_only=False, telegram_max_per_scan=5,
        telegram_min_interval_minutes=0, scan_history_days=30,
    )
    monkeypatch.setattr(telegram, "settings", configured)
    monkeypatch.setattr(main, "settings", configured)
    calls = []

    def serve(response=None):
        def post(url, json, timeout):
            calls.append((url, json))
            if isinstance(response, Exception):
                raise response
            return response or FakeResponse()

        monkeypatch.setattr(telegram.requests, "post", post)
        return calls

    return serve


def add(guid, score=200.0, freeleech=False, last_seen=SEEN_AT, title=None, url=None):
    with SessionLocal() as db:
        db.add(Opportunity(
            guid=guid, title=title or f"Titre {guid}", download_url=url, score=score, seeders=2, leechers=8,
            size_bytes=3 * GIB, freeleech=freeleech, last_seen_at=last_seen,
        ))
        db.commit()


def notification_count():
    with SessionLocal() as db:
        return db.scalar(select(func.count()).select_from(Notification))


def test_disabled_without_configuration(monkeypatch):
    add("a")
    monkeypatch.setattr(telegram.requests, "post", lambda *a, **k: pytest.fail("aucun appel attendu"))
    assert telegram.notify_opportunities(SEEN_AT) == 0


def test_message_describes_the_opportunity(bot):
    calls = bot()
    add("a", freeleech=True)
    assert telegram.notify_opportunities(SEEN_AT) == 1

    url, payload = calls[0]
    assert url == "https://api.telegram.org/bot123:secret/sendMessage"
    assert payload["chat_id"] == "42"
    for expected in ("Titre a", "Score 200", "S/L 2/8", "3.0 Go", "FreeLeech"):
        assert expected in payload["text"]
    assert "qBittorrent" not in payload["text"]


def test_title_is_escaped(bot):
    calls = bot()
    add("a", title="Film <1080p> & co")
    telegram.notify_opportunities(SEEN_AT)
    assert "Film &lt;1080p&gt; &amp; co" in calls[0][1]["text"]


def test_auto_grabbed_torrent_is_flagged(bot):
    calls = bot()
    add("a")
    with SessionLocal() as db:
        db.add(Grab(guid="a", title="Titre a", source="auto", status="sent"))
        db.commit()
    telegram.notify_opportunities(SEEN_AT)
    assert "Envoyé automatiquement" in calls[0][1]["text"]


def test_each_torrent_is_notified_once(bot):
    calls = bot()
    add("a")
    telegram.notify_opportunities(SEEN_AT)
    assert telegram.notify_opportunities(SEEN_AT) == 0
    assert len(calls) == 1


@pytest.mark.parametrize("fields", [{"score": 99}, {"last_seen": SEEN_AT - timedelta(minutes=1)}])
def test_ineligible_opportunities_are_ignored(bot, fields):
    calls = bot()
    add("a", **fields)
    assert telegram.notify_opportunities(SEEN_AT) == 0
    assert calls == []


def test_freeleech_filter(bot, monkeypatch):
    calls = bot()
    monkeypatch.setattr(telegram, "settings", replace(telegram.settings, telegram_freeleech_only=True))
    add("normal", score=300)
    add("free", score=200, freeleech=True)
    assert telegram.notify_opportunities(SEEN_AT) == 1
    assert "Titre free" in calls[0][1]["text"]


def test_best_scores_first_and_the_rest_at_the_next_scan(bot, monkeypatch):
    calls = bot()
    monkeypatch.setattr(telegram, "settings", replace(telegram.settings, telegram_max_per_scan=1))
    add("low", score=110)
    add("high", score=300)
    telegram.notify_opportunities(SEEN_AT)
    assert "Titre high" in calls[0][1]["text"]
    telegram.notify_opportunities(SEEN_AT)
    assert "Titre low" in calls[1][1]["text"]


def test_failure_keeps_the_torrent_for_the_next_scan(bot):
    bot(FakeResponse(429))
    add("a")
    assert telegram.notify_opportunities(SEEN_AT) == 0
    assert notification_count() == 0

    bot()
    assert telegram.notify_opportunities(SEEN_AT) == 1


def test_network_error_does_not_expose_the_token(bot, caplog):
    bot(requests.ConnectionError("https://api.telegram.org/bot123:secret/sendMessage"))
    add("a")
    assert telegram.notify_opportunities(SEEN_AT) == 0
    assert "secret" not in caplog.text


def test_old_notifications_are_purged(bot):
    bot()
    with SessionLocal() as db:
        db.add(Notification(guid="old", sent_at=SEEN_AT - timedelta(days=31)))
        db.commit()
    telegram.notify_opportunities(SEEN_AT)
    assert notification_count() == 0


class FakeService:
    def __init__(self, error=None):
        self.error = error
        self.urls = []

    def add_from_url(self, url):
        if self.error:
            raise self.error
        self.urls.append(url)


@pytest.fixture
def service(monkeypatch):
    fake = FakeService()
    monkeypatch.setattr(telegram, "get_qbittorrent", lambda: fake)
    return fake


def add_notified(guid="a"):
    add(guid)
    with SessionLocal() as db:
        db.scalar(select(Opportunity).where(Opportunity.guid == guid)).download_url = f"http://x/{guid}"
        notification = Notification(guid=guid)
        db.add(notification)
        db.commit()
        return notification.id


def click(notification_id, chat_id=42):
    return {
        "id": "cb1",
        "data": f"grab:{notification_id}",
        "message": {"message_id": 7, "chat": {"id": chat_id}},
    }


def methods(calls):
    return [url.rsplit("/", 1)[1] for url, _ in calls]


def answer_of(calls):
    return next(payload for url, payload in calls if url.endswith("answerCallbackQuery"))


def test_alert_carries_a_download_button(bot):
    calls = bot()
    add("a", url="http://x/a")
    telegram.notify_opportunities(SEEN_AT)
    with SessionLocal() as db:
        notification_id = db.scalar(select(Notification.id))
    button = calls[0][1]["reply_markup"]["inline_keyboard"][0][0]
    assert button["callback_data"] == f"grab:{notification_id}"


def test_no_button_without_download_url_or_when_already_sent(bot):
    calls = bot()
    add("nolink")
    add("sent", url="http://x/sent", score=150)
    with SessionLocal() as db:
        db.add(Grab(guid="sent", title="Titre", source="auto", status="sent"))
        db.commit()
    telegram.notify_opportunities(SEEN_AT)
    assert len(calls) == 1
    assert "reply_markup" not in calls[0][1]


def test_several_opportunities_share_one_message_with_one_button_each(bot):
    calls = bot()
    add("a", score=300, url="http://x/a")
    add("b", score=200, url="http://x/b")
    assert telegram.notify_opportunities(SEEN_AT) == 2

    assert len(calls) == 1
    text = calls[0][1]["text"]
    assert text.index("Titre a") < text.index("Titre b")
    rows = calls[0][1]["reply_markup"]["inline_keyboard"]
    assert [row[0]["text"] for row in rows] == ["⬇️ 1. Titre a", "⬇️ 2. Titre b"]


def test_second_alert_waits_for_the_interval(bot, monkeypatch):
    calls = bot()
    monkeypatch.setattr(telegram, "settings", replace(telegram.settings, telegram_min_interval_minutes=60))
    add("a")
    assert telegram.notify_opportunities(SEEN_AT) == 1

    soon = SEEN_AT + timedelta(minutes=30)
    add("b", last_seen=soon)
    assert telegram.notify_opportunities(soon) == 0
    assert len(calls) == 1

    later = SEEN_AT + timedelta(minutes=61)
    with SessionLocal() as db:
        db.scalar(select(Opportunity).where(Opportunity.guid == "b")).last_seen_at = later
        db.commit()
    assert telegram.notify_opportunities(later) == 1
    assert len(calls) == 2


def test_failed_alert_does_not_start_the_interval(bot, monkeypatch):
    bot(FakeResponse(500))
    monkeypatch.setattr(telegram, "settings", replace(telegram.settings, telegram_min_interval_minutes=60))
    add("a")
    assert telegram.notify_opportunities(SEEN_AT) == 0
    assert notification_count() == 0

    bot()
    assert telegram.notify_opportunities(SEEN_AT) == 1


def account(**overrides):
    return {
        "ratio": "12.50", "margin_gb": "64.0", "uploaded_gb": "54.7", "downloaded_gb": "4.4",
        "allowed": True, "blocked_reason": None, "below_reserve": False, **overrides,
    }


def test_alert_starts_with_the_ratio_recap(bot, monkeypatch):
    calls = bot()
    monkeypatch.setattr(telegram.c411, "summary", account)
    add("a")
    telegram.notify_opportunities(SEEN_AT)
    text = calls[0][1]["text"]
    assert "Ratio 12.50 · Marge 64.0 Go" in text
    assert "⬆️ 54.7 Go · ⬇️ 4.4 Go" in text
    assert text.index("Ratio") < text.index("Titre a")


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (account(below_reserve=True), "⚠️ 📊 Ratio"),
        (account(allowed=False, blocked_reason="insufficient_balance"), "insufficient_balance"),
        ({"error": "clé <invalide>"}, "clé &lt;invalide&gt;"),
    ],
)
def test_recap_flags_account_problems(bot, monkeypatch, state, expected):
    calls = bot()
    monkeypatch.setattr(telegram.c411, "summary", lambda: state)
    add("a")
    telegram.notify_opportunities(SEEN_AT)
    assert expected in calls[0][1]["text"]


@pytest.mark.parametrize("text", ["/ratio", "/ratio@spaceseed_bot", "/RATIO"])
def test_ratio_command_answers(bot, monkeypatch, text):
    calls = bot()
    monkeypatch.setattr(telegram.c411, "summary", account)
    telegram.handle_message({"text": text, "chat": {"id": 42}})
    assert "Ratio 12.50" in calls[0][1]["text"]


def test_ratio_command_without_c411(bot):
    calls = bot()
    telegram.handle_message({"text": "/ratio", "chat": {"id": 42}})
    assert "pas configuré" in calls[0][1]["text"]


@pytest.mark.parametrize(
    "message",
    [
        {"text": "/ratio", "chat": {"id": 666}},
        {"text": "bonjour", "chat": {"id": 42}},
        {"chat": {"id": 42}},
        {},
    ],
)
def test_other_messages_are_ignored(bot, message):
    calls = bot()
    telegram.handle_message(message)
    assert calls == []


def test_click_starts_the_download_and_removes_the_button(bot, service):
    calls = bot()
    telegram.handle_callback(click(add_notified()))
    assert service.urls == ["http://x/a"]
    assert methods(calls) == ["answerCallbackQuery", "editMessageReplyMarkup"]
    with SessionLocal() as db:
        assert db.scalar(select(Grab)).source == "telegram"


def test_click_keeps_the_other_buttons(bot, service):
    calls = bot()
    notification_id = add_notified()
    keyboard = {"inline_keyboard": [
        [{"text": "1", "callback_data": f"grab:{notification_id}"}],
        [{"text": "2", "callback_data": "grab:999"}],
    ]}
    callback = click(notification_id)
    callback["message"]["reply_markup"] = keyboard
    telegram.handle_callback(callback)

    edit = next(payload for url, payload in calls if url.endswith("editMessageReplyMarkup"))
    assert edit["reply_markup"] == {"inline_keyboard": [[{"text": "2", "callback_data": "grab:999"}]]}


def test_second_click_does_not_send_twice(bot, service):
    bot()
    notification_id = add_notified()
    telegram.handle_callback(click(notification_id))
    telegram.handle_callback(click(notification_id))
    assert len(service.urls) == 1


def test_click_from_another_chat_is_refused(bot, service):
    calls = bot()
    telegram.handle_callback(click(add_notified(), chat_id=666))
    assert service.urls == []
    assert answer_of(calls)["text"] == "Non autorisé"


@pytest.mark.parametrize("data", ["", "grab:", "grab:abc", "grab:1;drop", "autre:1"])
def test_invalid_callback_data_is_ignored(bot, service, data):
    calls = bot()
    add_notified()
    telegram.handle_callback({**click(1), "data": data})
    assert service.urls == []
    assert answer_of(calls)["text"] == "Action inconnue"


def test_click_on_a_removed_opportunity(bot, service):
    calls = bot()
    notification_id = add_notified()
    with SessionLocal() as db:
        db.delete(db.scalar(select(Opportunity)))
        db.commit()
    telegram.handle_callback(click(notification_id))
    assert service.urls == []
    assert "plus disponible" in answer_of(calls)["text"]


def test_click_stays_blocked_by_c411(bot, service, monkeypatch):
    calls = bot()
    monkeypatch.setattr(autograb, "download_block_reason", lambda *a: "Marge C411 insuffisante")
    telegram.handle_callback(click(add_notified()))
    assert service.urls == []
    assert answer_of(calls)["text"] == "Marge C411 insuffisante"
    assert answer_of(calls)["show_alert"] is True
    assert "editMessageReplyMarkup" not in methods(calls)


def test_qbittorrent_error_keeps_the_button(bot, monkeypatch):
    calls = bot()
    monkeypatch.setattr(telegram, "get_qbittorrent", lambda: FakeService(QBittorrentError("hors ligne")))
    telegram.handle_callback(click(add_notified()))
    assert answer_of(calls)["text"] == "hors ligne"
    assert "editMessageReplyMarkup" not in methods(calls)


class UpdatesResponse(FakeResponse):
    def __init__(self, updates):
        super().__init__()
        self.updates = updates

    def json(self):
        return {"result": self.updates}


def test_polling_advances_the_offset_and_survives_a_bad_update(bot, service):
    bot(UpdatesResponse([{"update_id": 5, "callback_query": {}}, {"update_id": 6, "other": 1}]))
    assert telegram.poll_updates(None) == 7


def test_polling_listens_to_messages_and_clicks(bot):
    calls = bot(UpdatesResponse([]))
    telegram.poll_updates(None)
    assert calls[0][1]["allowed_updates"] == ["callback_query", "message"]


def test_polling_keeps_the_offset_when_idle(bot):
    bot(UpdatesResponse([]))
    assert telegram.poll_updates(12) == 12


def test_polling_error_does_not_expose_the_token(bot):
    bot(requests.ConnectionError("https://api.telegram.org/bot123:secret/getUpdates"))
    with pytest.raises(telegram.TelegramError) as error:
        telegram.poll_updates(None)
    assert "secret" not in str(error.value)


def test_test_endpoint_sends_a_message(bot):
    calls = bot()
    assert TestClient(main.app).post("/telegram/test").status_code == 200
    assert len(calls) == 1


def test_test_endpoint_reports_a_refusal_without_the_token(bot):
    bot(FakeResponse(401))
    response = TestClient(main.app).post("/telegram/test")
    assert response.status_code == 502
    assert "secret" not in response.text


def test_test_endpoint_requires_configuration():
    assert TestClient(main.app).post("/telegram/test").status_code == 400
