from dataclasses import replace
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from app import autograb, main
from app.database import SessionLocal
from app.models import Grab, Opportunity
from app.qbittorrent import QBittorrentError, QBittorrentService

GIB = 1024 ** 3
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class FakeService:
    def __init__(self, active=0, free=1000 * GIB, refuse=(), list_error=None):
        self.active, self.free, self.refuse, self.list_error = active, free, refuse, list_error
        self.urls = []

    def list_torrents(self):
        if self.list_error:
            raise self.list_error
        return [{"progress": 0.5}] * self.active + [{"progress": 1.0}] * 3

    def free_space_bytes(self):
        return self.free

    def add_from_url(self, url):
        if url in self.refuse:
            raise QBittorrentError("refusé")
        self.urls.append(url)


@pytest.fixture
def configure(monkeypatch):
    def apply(service=None, **overrides):
        service = service or FakeService()
        options = dict(
            auto_grab_enabled=True, auto_grab_min_score=100, auto_grab_min_seeders=1,
            auto_grab_freeleech_only=True, auto_grab_max_size_gb=50, auto_grab_max_per_scan=2,
            auto_grab_max_active=5, auto_grab_min_free_space_gb=10,
        )
        monkeypatch.setattr(autograb, "settings", replace(autograb.settings, **{**options, **overrides}))
        monkeypatch.setattr(autograb, "get_qbittorrent", lambda: service)
        return service

    return apply


def add(guid, score=200.0, seeders=2, size_gb=1.0, freeleech=True, last_seen=NOW, url="auto"):
    with SessionLocal() as db:
        db.add(Opportunity(
            guid=guid, title=f"Titre {guid}", download_url=f"http://x/{guid}" if url else None,
            score=score, seeders=seeders, size_bytes=int(size_gb * GIB), freeleech=freeleech,
            first_seen_at=last_seen, last_seen_at=last_seen,
        ))
        db.commit()


def grab_status(guid):
    with SessionLocal() as db:
        grab = db.scalar(select(Grab).where(Grab.guid == guid))
        return (grab.status, grab.attempts) if grab else None


def test_disabled_by_default_sends_nothing(configure):
    service = configure(auto_grab_enabled=False)
    add("a")
    assert autograb.auto_grab(NOW) == 0
    assert service.urls == []


def test_sends_best_scores_first_up_to_the_scan_limit(configure):
    service = configure(auto_grab_max_per_scan=2)
    add("low", score=110)
    add("high", score=300)
    add("mid", score=200)
    assert autograb.auto_grab(NOW) == 2
    assert service.urls == ["http://x/high", "http://x/mid"]


@pytest.mark.parametrize(
    "fields",
    [
        {"score": 99},
        {"seeders": 0},
        {"freeleech": False},
        {"size_gb": 51},
        {"size_gb": 0},
        {"url": None},
        {"last_seen": NOW - timedelta(minutes=1)},
    ],
)
def test_ineligible_opportunities_are_skipped(configure, fields):
    service = configure()
    add("a", **fields)
    assert autograb.auto_grab(NOW) == 0
    assert service.urls == []


def test_non_freeleech_accepted_when_allowed(configure):
    configure(auto_grab_freeleech_only=False)
    add("a", freeleech=False)
    assert autograb.auto_grab(NOW) == 1


def test_already_sent_torrent_is_not_sent_again(configure):
    service = configure()
    add("a")
    autograb.auto_grab(NOW)
    assert autograb.auto_grab(NOW) == 0
    assert len(service.urls) == 1


def test_active_limit_reduces_the_available_slots(configure):
    service = configure(FakeService(active=4), auto_grab_max_active=5, auto_grab_max_per_scan=3)
    add("a", score=300)
    add("b", score=200)
    assert autograb.auto_grab(NOW) == 1
    assert service.urls == ["http://x/a"]


def test_active_limit_reached_sends_nothing(configure):
    service = configure(FakeService(active=5))
    add("a")
    assert autograb.auto_grab(NOW) == 0
    assert service.urls == []


def test_reserved_space_accounts_for_torrents_already_sent(configure):
    service = configure(FakeService(free=25 * GIB), auto_grab_min_free_space_gb=10)
    add("a", score=300, size_gb=10)
    add("b", score=200, size_gb=10)
    add("c", score=100, size_gb=4)
    assert autograb.auto_grab(NOW) == 2
    assert service.urls == ["http://x/a", "http://x/c"]


def test_refused_torrent_is_abandoned_after_three_attempts(configure):
    service = configure(FakeService(refuse={"http://x/a"}))
    add("a")
    for _ in range(autograb.MAX_ATTEMPTS):
        assert autograb.auto_grab(NOW) == 0
    assert grab_status("a") == ("failed", 3)

    add("b", score=100)
    assert autograb.auto_grab(NOW) == 1
    assert service.urls == ["http://x/b"]


def test_qbittorrent_offline_does_not_burn_attempts_or_raise(configure):
    configure(FakeService(list_error=QBittorrentError("hors ligne")))
    add("a")
    assert autograb.auto_grab(NOW) == 0
    assert grab_status("a") is None


def test_manual_download_blocks_the_automatic_one(configure, monkeypatch):
    service = configure()
    monkeypatch.setattr(main, "get_qbittorrent", lambda: service)
    add("a")
    with SessionLocal() as db:
        opportunity_id = db.scalar(select(Opportunity.id))

    client = TestClient(main.app, follow_redirects=False)
    assert client.post(f"/download/{opportunity_id}").status_code == 303
    assert grab_status("a") == ("sent", 1)
    assert autograb.auto_grab(NOW) == 0
    assert len(service.urls) == 1


def test_history_lists_grabs(configure):
    configure()
    add("a")
    autograb.auto_grab(NOW)
    page = TestClient(main.app).get("/history").text
    assert "Titre a" in page
    assert "Automatique" in page


def test_free_space_reads_the_qbittorrent_state(monkeypatch):
    service = QBittorrentService()
    monkeypatch.setattr(service.client, "sync_maindata", lambda: {"server_state": {"free_space_on_disk": 42}})
    assert service.free_space_bytes() == 42


def test_unknown_free_space_is_an_error(monkeypatch):
    service = QBittorrentService()
    monkeypatch.setattr(service.client, "sync_maindata", lambda: {})
    with pytest.raises(QBittorrentError, match="inconnu"):
        service.free_space_bytes()
