from dataclasses import replace
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app import scanner
from app.database import SessionLocal
from app.models import Opportunity, ScanRun
from app.torznab import TorznabError, TorznabItem


def make_item(guid: str = "a", leechers: int = 5, size_bytes: int = 1024) -> TorznabItem:
    return TorznabItem(
        guid=guid, title=f"Titre {guid}", download_url="http://x/1.torrent", category="2000",
        size_bytes=size_bytes, seeders=1, leechers=leechers, grabs=0, freeleech=False,
        download_factor=1.0, upload_factor=1.0, published_at=None,
    )


def serve(monkeypatch, items=None, error=None):
    def search(_self):
        if error:
            raise error
        return items

    monkeypatch.setattr(scanner.TorznabClient, "search", search)


def count(model) -> int:
    with SessionLocal() as db:
        return db.scalar(select(func.count()).select_from(model))


def test_duplicate_guid_in_one_feed_does_not_fail_the_scan(monkeypatch):
    serve(monkeypatch, [make_item(leechers=5), make_item(leechers=9)])
    result = scanner.scan_once()

    assert result["status"] == "success"
    assert (result["created"], result["updated"]) == (1, 1)
    with SessionLocal() as db:
        assert db.scalar(select(Opportunity)).leechers == 9


def test_rescan_updates_and_keeps_first_seen(monkeypatch):
    serve(monkeypatch, [make_item(leechers=5)])
    scanner.scan_once()
    with SessionLocal() as db:
        first_seen = db.scalar(select(Opportunity)).first_seen_at

    serve(monkeypatch, [make_item(leechers=8)])
    assert scanner.scan_once()["updated"] == 1
    with SessionLocal() as db:
        row = db.scalar(select(Opportunity))
    assert (row.leechers, row.first_seen_at) == (8, first_seen)


def test_torrent_seen_again_after_an_outage_is_kept(monkeypatch):
    long_ago = datetime.now(timezone.utc) - timedelta(days=2)
    with SessionLocal() as db:
        db.add(Opportunity(guid="a", title="Titre a", first_seen_at=long_ago, last_seen_at=long_ago))
        db.add(Opportunity(guid="gone", title="Disparu", first_seen_at=long_ago, last_seen_at=long_ago))
        db.commit()

    serve(monkeypatch, [make_item("a")])
    result = scanner.scan_once()

    assert (result["status"], result["updated"], result["stale_removed"]) == ("success", 1, 1)
    assert count(Opportunity) == 1


def test_filtered_item_ignored_while_stale_cleanup_runs(monkeypatch):
    serve(monkeypatch, [make_item()])
    scanner.scan_once()

    serve(monkeypatch, [make_item(leechers=0)])
    result = scanner.scan_once()

    assert (result["status"], result["rejected"]) == ("success", 1)


def test_large_torrent_size_round_trips(monkeypatch):
    size = 80 * 1024 ** 3
    serve(monkeypatch, [make_item(size_bytes=size)])
    scanner.scan_once()
    with SessionLocal() as db:
        assert db.scalar(select(Opportunity)).size_bytes == size


def test_failed_scan_is_recorded_and_keeps_opportunities(monkeypatch):
    serve(monkeypatch, [make_item()])
    scanner.scan_once()

    serve(monkeypatch, error=TorznabError("Erreur Torznab 100 : Invalid API Key"))
    result = scanner.scan_once()

    assert result["status"] == "error"
    assert count(Opportunity) == 1
    with SessionLocal() as db:
        last = db.scalars(select(ScanRun).order_by(ScanRun.id.desc())).first()
    assert (last.status, last.error) == ("error", "Erreur Torznab 100 : Invalid API Key")


def test_lock_is_released_when_the_database_fails(monkeypatch):
    def broken_session():
        raise_on_commit = SessionLocal()
        raise_on_commit.commit = lambda: (_ for _ in ()).throw(RuntimeError("disque plein"))
        return raise_on_commit

    monkeypatch.setattr(scanner, "SessionLocal", broken_session)
    assert scanner.scan_once()["status"] == "error"
    assert not scanner._scan_lock.locked()


def test_old_scan_history_is_purged(monkeypatch):
    monkeypatch.setattr(scanner, "settings", replace(scanner.settings, scan_history_days=30))
    with SessionLocal() as db:
        db.add(ScanRun(started_at=datetime.now(timezone.utc) - timedelta(days=31), status="success"))
        db.commit()

    serve(monkeypatch, [])
    scanner.scan_once()
    assert count(ScanRun) == 1
