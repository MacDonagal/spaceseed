from dataclasses import replace
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
import pytest
import requests
from sqlalchemy import func, select

from app import main, telegram
from app.database import SessionLocal
from app.models import Grab, Notification, Opportunity

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
        scan_history_days=30,
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


def add(guid, score=200.0, freeleech=False, last_seen=SEEN_AT, title=None):
    with SessionLocal() as db:
        db.add(Opportunity(
            guid=guid, title=title or f"Titre {guid}", score=score, seeders=2, leechers=8,
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
