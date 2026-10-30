import base64
from dataclasses import replace

from fastapi.testclient import TestClient
import pytest
import qbittorrentapi

from app import main, qbittorrent
from app.database import SessionLocal
from app.models import Opportunity
from app.qbittorrent import QBittorrentError, QBittorrentService, swarm_health


@pytest.fixture
def client():
    # Sans bloc « with » : le scheduler et son scan immédiat ne démarrent pas.
    return TestClient(main.app, follow_redirects=False)


@pytest.fixture
def protected(monkeypatch):
    monkeypatch.setattr(main, "settings", replace(main.settings, spaceseed_username="me", spaceseed_password="pw"))


def basic(user: str, password: str) -> dict:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


class FakeService:
    def __init__(self, error=None):
        self.error = error
        self.urls = []

    def add_from_url(self, url):
        if self.error:
            raise self.error
        self.urls.append(url)

    def list_torrents(self):
        if self.error:
            raise self.error
        return [dict(TORRENT, added_on=1)]


TORRENT = {
    "name": "x", "state": "stalledDL", "progress": 0.5, "download_speed": 0, "upload_speed": 0,
    "seeds": 0, "availability": 0.5, "added_on": 0,
}


def add_opportunity(download_url="http://x/1.torrent?passkey=abc") -> int:
    with SessionLocal() as db:
        row = Opportunity(guid="a", title="Titre", download_url=download_url)
        db.add(row)
        db.commit()
        return row.id


@pytest.mark.parametrize("path", ["/", "/opportunities", "/history"])
def test_pages_render(client, path):
    assert client.get(path).status_code == 200


def test_auth_required_when_a_password_is_set(client, protected):
    response = client.get("/")
    assert response.status_code == 401
    assert "Basic" in response.headers["www-authenticate"]


def test_auth_rejects_bad_credentials(client, protected):
    assert client.get("/", headers=basic("me", "nope")).status_code == 401


def test_auth_accepts_good_credentials(client, protected):
    assert client.get("/", headers=basic("me", "pw")).status_code == 200


def test_health_stays_public(client, protected):
    assert client.get("/health").status_code == 200


def test_cross_site_post_is_refused(client, monkeypatch):
    monkeypatch.setattr(main, "scan_once", lambda: pytest.fail("le scan ne doit pas démarrer"))
    assert client.post("/scan", headers={"Origin": "https://evil.example"}).status_code == 403


def test_same_origin_post_is_accepted(client, monkeypatch):
    monkeypatch.setattr(main, "scan_once", lambda: {"status": "success"})
    response = client.post("/scan", headers={"Origin": "http://testserver"})
    assert (response.status_code, response.headers["location"]) == (303, "/opportunities")


def test_failed_manual_scan_redirects_to_history(client, monkeypatch):
    monkeypatch.setattr(main, "scan_once", lambda: {"status": "error", "error": "boom"})
    assert client.post("/scan").headers["location"] == "/history"


def test_download_sends_the_url(client, monkeypatch):
    service = FakeService()
    monkeypatch.setattr(main, "get_qbittorrent", lambda: service)
    assert client.post(f"/download/{add_opportunity()}").status_code == 303
    assert service.urls == ["http://x/1.torrent?passkey=abc"]


def test_download_error_returns_the_reason(client, monkeypatch):
    monkeypatch.setattr(main, "get_qbittorrent", lambda: FakeService(QBittorrentError("refusé")))
    response = client.post(f"/download/{add_opportunity()}")
    assert (response.status_code, response.json()["detail"]) == (502, "refusé")


def test_download_unknown_opportunity(client):
    assert client.post("/download/999").status_code == 404


def test_torrents_endpoint_adds_health(client, monkeypatch):
    monkeypatch.setattr(main, "get_qbittorrent", lambda: FakeService())
    torrent = client.get("/api/qbittorrent/torrents").json()["torrents"][0]
    assert (torrent["health"], torrent["health_label"]) == ("slow", "Swarm lent")


def test_torrents_endpoint_reports_unavailable_qbittorrent(client, monkeypatch):
    monkeypatch.setattr(main, "get_qbittorrent", lambda: FakeService(QBittorrentError("hors ligne")))
    assert client.get("/api/qbittorrent/torrents").status_code == 502


def test_list_torrents_maps_the_qbittorrent_fields(monkeypatch):
    service = QBittorrentService()
    monkeypatch.setattr(service.client, "torrents_info", lambda **kwargs: [
        {"name": "x", "state": "uploading", "progress": 1, "dlspeed": 1, "upspeed": 2, "num_seeds": 3},
    ])
    torrent = service.list_torrents()[0]
    assert (torrent["download_speed"], torrent["upload_speed"], torrent["seeds"]) == (1, 2, 3)


def test_add_raises_when_qbittorrent_answers_fails(monkeypatch):
    service = QBittorrentService()
    monkeypatch.setattr(service.client, "torrents_add", lambda **kwargs: "Fails.")
    with pytest.raises(QBittorrentError, match="refusé"):
        service.add_from_url("http://x/1.torrent")


def test_wrong_credentials_become_a_clear_error(monkeypatch):
    service = QBittorrentService()

    def refuse(**kwargs):
        raise qbittorrentapi.LoginFailed()

    monkeypatch.setattr(service.client, "torrents_info", refuse)
    with pytest.raises(QBittorrentError, match="identifiants"):
        service.list_torrents()


def test_service_is_shared_between_calls():
    qbittorrent.get_qbittorrent.cache_clear()
    assert qbittorrent.get_qbittorrent() is qbittorrent.get_qbittorrent()


@pytest.mark.parametrize(
    ("overrides", "age", "expected"),
    [
        ({"state": "error"}, 9999, "blocked"),
        ({"state": "missingFiles", "progress": 1}, 9999, "blocked"),
        ({"state": "stoppedUP", "progress": 1}, 9999, "paused"),
        ({"state": "queuedDL"}, 9999, "waiting"),
        ({"state": "uploading", "progress": 1, "upload_speed": 5}, 9999, "seeding_active"),
        ({"state": "stalledUP", "progress": 1}, 9999, "seeding_idle"),
        ({"state": "downloading", "download_speed": 5}, 9999, "active"),
        ({}, 10, "waiting"),
        ({"progress": 0, "seeds": 3}, 1000, "blocked"),
        ({"availability": 0}, 1000, "blocked"),
        ({}, 1000, "slow"),
    ],
)
def test_swarm_health(overrides, age, expected):
    assert swarm_health({**TORRENT, **overrides}, age)[0] == expected
