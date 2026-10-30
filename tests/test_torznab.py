from datetime import timedelta
from pathlib import Path

import pytest
import requests

from app import torznab
from app.config import settings
from app.torznab import TorznabClient, TorznabError


def feed(*items: str) -> bytes:
    body = "".join(items)
    return (
        '<rss xmlns:torznab="http://torznab.com/schemas/2015/feed"><channel>'
        f"{body}</channel></rss>"
    ).encode()


def item(guid: str = "a", attrs: dict | None = None, date: str = "Mon, 05 Jan 2026 12:00:00 +0200") -> str:
    tags = "".join(
        f'<torznab:attr name="{name}" value="{value}"/>'
        for name, value in (attrs or {"seeders": 2, "peers": 7}).items()
    )
    return f"<item><title>Titre {guid}</title><guid>{guid}</guid><pubDate>{date}</pubDate>{tags}</item>"


def fake_response(content: bytes, status: int = 200) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response._content = content
    response.url = "http://indexer.test/api?apikey=secret-key&t=search"
    return response


def serve(monkeypatch, content: bytes, status: int = 200) -> None:
    monkeypatch.setattr(torznab.requests, "get", lambda *a, **k: fake_response(content, status))


def test_peers_include_seeders(monkeypatch):
    serve(monkeypatch, feed(item(attrs={"seeders": 2, "peers": 7})))
    assert TorznabClient().search()[0].leechers == 5


def test_explicit_leechers_take_priority(monkeypatch):
    serve(monkeypatch, feed(item(attrs={"seeders": 2, "peers": 7, "leechers": 3})))
    assert TorznabClient().search()[0].leechers == 3


def test_peers_lower_than_seeders_never_goes_negative(monkeypatch):
    serve(monkeypatch, feed(item(attrs={"seeders": 9, "peers": 2})))
    assert TorznabClient().search()[0].leechers == 0


def test_dates_are_normalized_to_utc(monkeypatch):
    serve(monkeypatch, feed(item()))
    published = TorznabClient().search()[0].published_at
    assert published.utcoffset() == timedelta(0)
    assert published.hour == 10


def test_indexer_error_is_not_an_empty_scan(monkeypatch):
    serve(monkeypatch, b'<error code="100" description="Invalid API Key"/>')
    with pytest.raises(TorznabError, match="Invalid API Key"):
        TorznabClient().search()


def test_http_error_does_not_expose_the_api_key(monkeypatch):
    serve(monkeypatch, b"oops", status=500)
    with pytest.raises(TorznabError) as error:
        TorznabClient().search()
    assert "secret-key" not in str(error.value)
    assert "apikey=***" in str(error.value)


def test_connection_error_does_not_expose_the_api_key(monkeypatch):
    def refuse(*args, **kwargs):
        raise requests.ConnectionError("Max retries: /api?apikey=secret-key&t=search")

    monkeypatch.setattr(torznab.requests, "get", refuse)
    with pytest.raises(TorznabError) as error:
        TorznabClient().search()
    assert "secret-key" not in str(error.value)


def test_invalid_xml_dumps_are_rotated(monkeypatch):
    serve(monkeypatch, b"<rss><unclosed>")
    for _ in range(torznab.INVALID_XML_KEPT + 3):
        with pytest.raises(Exception):
            TorznabClient().search()
    dumps = list(Path(settings.data_dir).glob("torznab-invalid-*.xml"))
    assert len(dumps) == torznab.INVALID_XML_KEPT
