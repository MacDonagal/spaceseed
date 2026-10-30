from dataclasses import dataclass
import logging
import threading
import time

import requests

from .config import settings

logger = logging.getLogger(__name__)
# Largement sous la limite de 30 requêtes par minute ; l'API ne rafraîchit
# ses données que toutes les 30 secondes.
CACHE_SECONDS = 60
# Règle affichée par C411 : blocage quand l'upload passe sous 80 % du download.
BLOCK_RATIO = 0.8
GIB = 1024 ** 3
HTTP_ERRORS = {
    401: "clé API absente ou invalide",
    403: "la clé API n'a pas le scope « Profil (lecture) »",
    429: "limite de requêtes atteinte",
}

_lock = threading.Lock()
_cached: tuple[float, "C411Status | C411Error"] | None = None


class C411Error(RuntimeError):
    pass


@dataclass(frozen=True)
class C411Status:
    uploaded: int
    downloaded: int
    allowed: bool
    blocked_reason: str | None
    freeleech_active: bool

    @property
    def ratio(self) -> float:
        return self.uploaded / self.downloaded if self.downloaded > 0 else float("inf")

    @property
    def margin_bytes(self) -> float:
        return self.uploaded / BLOCK_RATIO - self.downloaded


def _fetch_status() -> C411Status:
    try:
        response = requests.get(
            settings.c411_api_url,
            headers={"Authorization": f"Bearer {settings.c411_api_key}"},
            timeout=settings.request_timeout_seconds,
        )
    except requests.RequestException as exc:
        raise C411Error(f"API injoignable : {exc}") from None

    if response.status_code != 200:
        detail = HTTP_ERRORS.get(response.status_code, f"HTTP {response.status_code}")
        raise C411Error(detail)

    try:
        profile = response.json()
        return C411Status(
            uploaded=int(profile["transfer"]["uploaded"]["total"]),
            downloaded=int(profile["transfer"]["downloaded"]["total"]),
            # Seul un « true » explicite autorise le téléchargement.
            allowed=profile["download"]["allowed"] is True,
            blocked_reason=profile["download"].get("blockedReason"),
            freeleech_active=(profile.get("freeleech") or {}).get("active") is True,
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        raise C411Error("réponse illisible") from None


def get_status() -> C411Status:
    global _cached
    with _lock:
        now = time.monotonic()
        if _cached is None or _cached[0] <= now:
            try:
                result = _fetch_status()
            except C411Error as exc:
                result = exc
            # Les échecs aussi sont gardés : sinon chaque candidat d'un scan
            # relancerait une requête et rapprocherait d'un 429.
            _cached = (now + CACHE_SECONDS, result)

        result = _cached[1]
    if isinstance(result, C411Error):
        raise result
    return result


def reset_cache() -> None:
    global _cached
    with _lock:
        _cached = None


def download_block_reason(size_bytes: int, freeleech: bool, pending_bytes: int = 0) -> str | None:
    """Raison de refuser un téléchargement, ou None s'il peut partir."""
    if not settings.c411_api_key:
        return None

    try:
        status = get_status()
    except C411Error as exc:
        return f"Vérification C411 impossible : {exc}"

    if not status.allowed:
        return f"Téléchargement bloqué par C411 : {status.blocked_reason or 'raison inconnue'}"

    # Un torrent gratuit ne consomme pas de marge, même si elle est déjà faible.
    if freeleech or status.freeleech_active:
        return None

    remaining = (status.margin_bytes - pending_bytes - size_bytes) / GIB
    if remaining < settings.c411_min_margin_gb:
        return (
            f"Marge C411 insuffisante : {remaining:.1f} Go restants après ce torrent "
            f"(réserve {settings.c411_min_margin_gb:g} Go)"
        )
    return None


def summary() -> dict | None:
    if not settings.c411_api_key:
        return None
    try:
        status = get_status()
    except C411Error as exc:
        return {"error": str(exc)}

    margin_gb = status.margin_bytes / GIB
    return {
        "ratio": "∞" if status.ratio == float("inf") else f"{status.ratio:.2f}",
        "margin_gb": f"{margin_gb:.1f}",
        "min_margin_gb": f"{settings.c411_min_margin_gb:g}",
        "allowed": status.allowed,
        "blocked_reason": status.blocked_reason,
        "below_reserve": margin_gb < settings.c411_min_margin_gb,
    }
