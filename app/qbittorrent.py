from contextlib import contextmanager
from functools import lru_cache
import logging
import requests
import qbittorrentapi

from .config import settings

logger = logging.getLogger(__name__)

PAUSED_STATES = {"pauseddl", "pausedup", "stoppeddl", "stoppedup"}
ERROR_STATES = {"error", "missingfiles"}
TRANSIENT_STATES = {
    "allocating", "checkingdl", "checkingresumedata", "checkingup",
    "moving", "queueddl", "queuedup",
}


class QBittorrentError(RuntimeError):
    pass


def swarm_health(torrent: dict, age: int) -> tuple[str, str]:
    progress, down, up = torrent["progress"], torrent["download_speed"], torrent["upload_speed"]
    state, seeds, availability = torrent["state"].lower(), torrent["seeds"], torrent["availability"]

    if state in ERROR_STATES:
        return "blocked", "Erreur"
    if state in PAUSED_STATES:
        return "paused", "En pause"
    if state in TRANSIENT_STATES:
        return "waiting", "En attente"
    if progress >= 1:
        return ("seeding_active", "Seed actif") if up > 0 else ("seeding_idle", "En partage")
    if down > 0:
        return "active", "Actif"
    if age < 300:
        return "waiting", "Démarrage"
    if progress == 0 and age >= 900 and seeds > 0:
        return "blocked", "Swarm bloqué"
    if availability == 0:
        return "blocked", "Indisponible"
    return "slow", "Swarm lent"


@contextmanager
def _api_errors():
    try:
        yield
    except qbittorrentapi.LoginFailed as exc:
        raise QBittorrentError("Connexion qBittorrent refusée : identifiants invalides") from exc
    except requests.RequestException as exc:
        raise QBittorrentError(f"qBittorrent inaccessible : {exc}") from exc
    except qbittorrentapi.APIError as exc:
        raise QBittorrentError(f"Erreur API qBittorrent : {exc}") from exc


class QBittorrentService:
    def __init__(self) -> None:
        if not settings.qbittorrent_url:
            raise QBittorrentError("QBITTORRENT_URL n'est pas configurée")

        self.client = qbittorrentapi.Client(
            host=settings.qbittorrent_url,
            username=settings.qbittorrent_username,
            password=settings.qbittorrent_password,
            REQUESTS_ARGS={"timeout": 30},
        )

    def version(self) -> str:
        with _api_errors():
            self.client.auth_log_in()
            return self.client.app_version()

    def add_from_url(self, torrent_url: str) -> None:
        options = {
            "urls": torrent_url,
            "category": settings.qbittorrent_category or None,
            "is_paused": settings.qbittorrent_paused,
        }
        if settings.qbittorrent_save_path:
            options["save_path"] = settings.qbittorrent_save_path

        with _api_errors():
            result = self.client.torrents_add(**options)

        # L'API répond HTTP 200 avec « Fails. » quand le torrent est refusé.
        if str(result).strip().lower().startswith("fails"):
            raise QBittorrentError("qBittorrent a refusé le torrent")

    def free_space_bytes(self) -> int:
        with _api_errors():
            state = self.client.sync_maindata().get("server_state", {})
        if "free_space_on_disk" not in state:
            raise QBittorrentError("Espace disque qBittorrent inconnu")
        return int(state["free_space_on_disk"])

    def list_torrents(self) -> list[dict]:
        category = settings.qbittorrent_category or None
        with _api_errors():
            torrents = self.client.torrents_info(category=category)

        return [{
            "hash": t.get("hash", ""), "name": t.get("name", ""), "state": t.get("state", "unknown"),
            "progress": float(t.get("progress", 0)), "download_speed": int(t.get("dlspeed", 0)),
            "upload_speed": int(t.get("upspeed", 0)), "ratio": float(t.get("ratio", 0)),
            "seeds": int(t.get("num_seeds", 0)), "leechers": int(t.get("num_leechs", 0)),
            "availability": float(t.get("availability", -1)), "added_on": int(t.get("added_on", 0)),
            "last_activity": int(t.get("last_activity", 0))
        } for t in torrents]


@lru_cache(maxsize=1)
def get_qbittorrent() -> QBittorrentService:
    # Instance partagée : la grille interroge qBittorrent toutes les 15 secondes
    # et un nouveau client ouvrirait une nouvelle session à chaque appel.
    return QBittorrentService()
