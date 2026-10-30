from dataclasses import replace
from datetime import datetime, timezone

from fastapi.testclient import TestClient
import pytest
import requests

from app import autograb, c411, main
from app.database import SessionLocal
from app.models import Opportunity

GIB = 1024 ** 3


def profile(uploaded=150 * GIB, downloaded=100 * GIB, allowed=True, reason=None, freeleech=False) -> dict:
    return {
        "transfer": {
            "uploaded": {"actual": uploaded, "credit": 0, "total": uploaded},
            "downloaded": {"actual": downloaded, "credit": 0, "total": downloaded},
        },
        "download": {"allowed": allowed, "blockedReason": reason},
        "freeleech": {"active": freeleech, "sources": []},
    }


class FakeResponse:
    def __init__(self, payload=None, status_code=200):
        self.payload, self.status_code = payload, status_code

    def json(self):
        if self.payload is None:
            raise ValueError("pas de JSON")
        return self.payload


@pytest.fixture
def api(monkeypatch):
    c411.reset_cache()
    monkeypatch.setattr(c411, "settings", replace(c411.settings, c411_api_key="key", c411_min_margin_gb=10))
    calls = []

    def serve(response):
        def get(url, headers, timeout):
            calls.append(headers)
            if isinstance(response, Exception):
                raise response
            return response

        monkeypatch.setattr(c411.requests, "get", get)
        return calls

    yield serve
    c411.reset_cache()


def test_disabled_without_a_key_makes_no_request(monkeypatch):
    c411.reset_cache()
    monkeypatch.setattr(c411.requests, "get", lambda *a, **k: pytest.fail("aucun appel attendu"))
    assert c411.download_block_reason(GIB, False) is None
    assert c411.summary() is None


def test_uses_the_bearer_key(api):
    calls = api(FakeResponse(profile()))
    assert c411.download_block_reason(GIB, False) is None
    assert calls == [{"Authorization": "Bearer key"}]


def test_credits_are_counted_through_totals(api):
    payload = profile()
    payload["transfer"]["uploaded"]["total"] = 300 * GIB
    api(FakeResponse(payload))
    assert c411.get_status().ratio == 3.0


def test_size_is_projected_on_a_normal_torrent(api):
    api(FakeResponse(profile()))
    assert c411.download_block_reason(77 * GIB, False) is None
    assert "Marge C411 insuffisante" in c411.download_block_reason(78 * GIB, False)


def test_margin_matches_the_value_shown_by_c411(api):
    api(FakeResponse(profile(uploaded=int(54.7 * GIB), downloaded=int(4.4 * GIB))))
    assert round(c411.get_status().margin_bytes / GIB) == 64


def test_pending_downloads_are_projected(api):
    api(FakeResponse(profile()))
    assert "insuffisante" in c411.download_block_reason(40 * GIB, False, pending_bytes=40 * GIB)


def test_freeleech_torrent_ignores_its_size(api):
    api(FakeResponse(profile()))
    assert c411.download_block_reason(500 * GIB, True) is None


def test_global_freeleech_ignores_the_size(api):
    api(FakeResponse(profile(freeleech=True)))
    assert c411.download_block_reason(500 * GIB, False) is None


def test_exhausted_margin_still_allows_freeleech(api):
    api(FakeResponse(profile(uploaded=50 * GIB)))
    assert c411.download_block_reason(GIB, True) is None
    assert "insuffisante" in c411.download_block_reason(GIB, False)


def test_no_download_yet_is_an_infinite_ratio(api):
    api(FakeResponse(profile(uploaded=0, downloaded=0)))
    assert c411.summary()["ratio"] == "∞"


def test_blocked_account_refuses_even_freeleech(api):
    api(FakeResponse(profile(allowed=False, reason="insufficient_balance")))
    assert "insufficient_balance" in c411.download_block_reason(GIB, True)


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (FakeResponse(status_code=401), "invalide"),
        (FakeResponse(status_code=403), "scope"),
        (FakeResponse(status_code=429), "limite"),
        (FakeResponse(status_code=500), "HTTP 500"),
        (FakeResponse(payload=None), "illisible"),
        (FakeResponse({"transfer": {}}), "illisible"),
        (FakeResponse(["inattendu"]), "illisible"),
        (requests.ConnectionError("réseau"), "injoignable"),
    ],
)
def test_any_failure_blocks_downloads(api, response, message):
    api(response)
    reason = c411.download_block_reason(GIB, True)
    assert "impossible" in reason
    assert message in reason


def test_allowed_must_be_exactly_true(api):
    payload = profile()
    payload["download"]["allowed"] = "true"
    api(FakeResponse(payload))
    assert "bloqué" in c411.download_block_reason(GIB, True)


def test_success_and_failure_are_cached(api):
    calls = api(FakeResponse(profile()))
    c411.get_status()
    c411.get_status()
    assert len(calls) == 1

    c411.reset_cache()
    calls = api(FakeResponse(status_code=429))
    for _ in range(3):
        c411.download_block_reason(GIB, False)
    assert len(calls) == 2


SEEN_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)


def add_opportunity(guid, score, size_gb=30, freeleech=False):
    with SessionLocal() as db:
        row = Opportunity(
            guid=guid, title=guid, download_url=f"http://x/{guid}", score=score, seeders=2,
            size_bytes=size_gb * GIB, freeleech=freeleech, last_seen_at=SEEN_AT,
        )
        db.add(row)
        db.commit()
        return row.id


class FakeService:
    def __init__(self):
        self.urls = []

    def list_torrents(self):
        return []

    def free_space_bytes(self):
        return 1000 * GIB

    def add_from_url(self, url):
        self.urls.append(url)


@pytest.fixture
def grabbing(monkeypatch):
    service = FakeService()
    monkeypatch.setattr(autograb, "get_qbittorrent", lambda: service)
    monkeypatch.setattr(main, "get_qbittorrent", lambda: service)
    monkeypatch.setattr(autograb, "settings", replace(
        autograb.settings, auto_grab_enabled=True, auto_grab_min_score=1, auto_grab_freeleech_only=False,
        auto_grab_max_per_scan=5, auto_grab_max_active=5, auto_grab_min_free_space_gb=0,
        auto_grab_max_size_gb=100,
    ))
    return service


def test_auto_grab_stops_before_the_ratio_limit(api, grabbing):
    api(FakeResponse(profile()))
    add_opportunity("a", 300)
    add_opportunity("b", 200)
    add_opportunity("c", 100)
    assert autograb.auto_grab(SEEN_AT) == 2
    assert grabbing.urls == ["http://x/a", "http://x/b"]


def test_auto_grab_skips_a_big_torrent_for_a_freeleech_one(api, grabbing):
    api(FakeResponse(profile()))
    add_opportunity("big", 300, size_gb=80)
    add_opportunity("free", 100, size_gb=80, freeleech=True)
    assert autograb.auto_grab(SEEN_AT) == 1
    assert grabbing.urls == ["http://x/free"]


def test_blocked_auto_grab_does_not_burn_attempts(api, grabbing):
    api(FakeResponse(status_code=500))
    add_opportunity("a", 300)
    for _ in range(5):
        assert autograb.auto_grab(SEEN_AT) == 0

    c411.reset_cache()
    api(FakeResponse(profile()))
    assert autograb.auto_grab(SEEN_AT) == 1


def test_manual_download_is_blocked_with_the_reason(api, grabbing):
    api(FakeResponse(profile(uploaded=50 * GIB)))
    opportunity_id = add_opportunity("a", 300)
    response = TestClient(main.app).post(f"/download/{opportunity_id}")
    assert response.status_code == 409
    assert "insuffisante" in response.json()["detail"]
    assert grabbing.urls == []


def test_dashboard_shows_the_ratio_card(api):
    api(FakeResponse(profile()))
    page = TestClient(main.app).get("/").text
    assert "Ratio C411" in page
    assert "Marge 87.5 Go" in page


def test_dashboard_stays_available_when_the_api_fails(api):
    api(FakeResponse(status_code=401))
    page = TestClient(main.app).get("/")
    assert page.status_code == 200
    assert "Vérification impossible" in page.text


def test_dashboard_hides_the_card_without_a_key(monkeypatch):
    c411.reset_cache()
    assert "Ratio C411" not in TestClient(main.app).get("/").text


def test_history_shows_the_ratio(api):
    api(FakeResponse(profile()))
    page = TestClient(main.app).get("/history").text
    assert "ratio 1.50, marge de 87.5 Go" in page


def test_history_stays_available_when_the_api_fails(api):
    api(FakeResponse(status_code=401))
    page = TestClient(main.app).get("/history")
    assert page.status_code == 200
    assert "vérification impossible" in page.text
