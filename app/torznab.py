from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
import logging
import re
import time
import xml.etree.ElementTree as ET

import requests

from .config import settings

logger = logging.getLogger(__name__)
TORZNAB_NS = "http://torznab.com/schemas/2015/feed"
INVALID_XML_KEPT = 5


class TorznabError(RuntimeError):
    pass


def _redact(text: str) -> str:
    # Les messages de requests contiennent l'URL complète, clé API comprise,
    # et finissent dans les logs et sur la page Historique.
    return re.sub(r"(?i)(apikey=)[^&\s'\")]+", r"\1***", text)


@dataclass
class TorznabItem:
    guid: str
    title: str
    download_url: str | None
    category: str | None
    size_bytes: int
    seeders: int
    leechers: int
    grabs: int
    freeleech: bool
    download_factor: float
    upload_factor: float
    published_at: datetime | None


def _number(value: str | None, cast, default):
    try:
        return cast(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _parse_date(value: str) -> datetime:
    parsed = parsedate_to_datetime(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _leechers(attrs: dict[str, str], seeders: int) -> int:
    if attrs.get("leechers") not in (None, ""):
        return _number(attrs["leechers"], int, 0)
    # Torznab : « peers » compte tout le swarm, seeders inclus.
    return max(_number(attrs.get("peers"), int, 0) - seeders, 0)


def _save_invalid_response(content: bytes) -> Path:
    directory = Path(settings.data_dir)
    target = directory / f"torznab-invalid-{time.time_ns()}.xml"
    target.write_bytes(content)
    for old in sorted(directory.glob("torznab-invalid-*.xml"))[:-INVALID_XML_KEPT]:
        old.unlink(missing_ok=True)
    return target


def _attributes(item: ET.Element) -> dict[str, str]:
    result: dict[str, str] = {}
    for attr in item.findall(f"{{{TORZNAB_NS}}}attr"):
        name = attr.attrib.get("name")
        value = attr.attrib.get("value")
        if name and value is not None:
            result[name.lower()] = value
    return result


class TorznabClient:
    def search(self) -> list[TorznabItem]:
        if not settings.torznab_url:
            raise RuntimeError("TORZNAB_URL n'est pas configurée")
        if not settings.torznab_api_key:
            raise RuntimeError("TORZNAB_API_KEY n'est pas configurée")

        params = {
            "apikey": settings.torznab_api_key,
            "t": "search",
            "limit": settings.torznab_limit,
        }
        if settings.torznab_categories.strip():
            params["cat"] = settings.torznab_categories.strip()

        try:
            response = requests.get(
                settings.torznab_url,
                params=params,
                timeout=settings.request_timeout_seconds,
                headers={"Accept": "application/rss+xml, application/xml, text/xml"},
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise TorznabError(_redact(str(exc))) from None

        logger.info(
            "Réponse Torznab | status=%s | type=%s | taille=%s",
            response.status_code,
            response.headers.get("content-type"),
            len(response.content),
        )

        try:
            root = ET.fromstring(response.content)
        except ET.ParseError:
            target = _save_invalid_response(response.content)
            preview = response.text[:500].replace("\n", " ")
            logger.exception("XML Torznab invalide | sauvegarde=%s | début=%r", target, preview)
            raise

        # Sans ce contrôle, une erreur de l'indexeur passerait pour un scan vide
        # et toutes les opportunités finiraient retirées comme périmées.
        if root.tag == "error":
            code = root.attrib.get("code", "?")
            description = root.attrib.get("description", "erreur inconnue")
            raise TorznabError(_redact(f"Erreur Torznab {code} : {description}"))

        results: list[TorznabItem] = []
        for item in root.findall(".//item"):
            attrs = _attributes(item)
            title = (item.findtext("title") or "").strip()
            guid = (item.findtext("guid") or item.findtext("link") or title).strip()
            enclosure = item.find("enclosure")
            download_url = enclosure.attrib.get("url") if enclosure is not None else item.findtext("link")
            pub_date = item.findtext("pubDate")

            published_at = None
            if pub_date:
                try:
                    published_at = _parse_date(pub_date)
                except (TypeError, ValueError):
                    logger.warning("Date Torznab illisible | guid=%s | date=%r", guid, pub_date)

            download_factor = _number(attrs.get("downloadvolumefactor"), float, 1.0)
            upload_factor = _number(attrs.get("uploadvolumefactor"), float, 1.0)
            freeleech = download_factor == 0.0 or attrs.get("freeleech", "").lower() in {"1", "true", "yes"}

            if not guid or not title:
                continue

            seeders = _number(attrs.get("seeders"), int, 0)
            results.append(
                TorznabItem(
                    guid=guid,
                    title=title,
                    download_url=download_url,
                    category=attrs.get("category") or item.findtext("category"),
                    size_bytes=_number(attrs.get("size") or (enclosure.attrib.get("length") if enclosure is not None else None), int, 0),
                    seeders=seeders,
                    leechers=_leechers(attrs, seeders),
                    grabs=_number(attrs.get("grabs"), int, 0),
                    freeleech=freeleech,
                    download_factor=download_factor,
                    upload_factor=upload_factor,
                    published_at=published_at,
                )
            )

        logger.info("Résultats Torznab reçus : %d", len(results))
        return results
