import base64
import binascii
import hmac
from urllib.parse import urlsplit


def basic_auth_valid(header: str | None, username: str, password: str) -> bool:
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:].strip(), validate=True)
    except (binascii.Error, ValueError):
        return False

    given_user, separator, given_password = decoded.partition(b":")
    # Les deux comparaisons sont toujours exécutées pour garder un temps constant.
    user_ok = hmac.compare_digest(given_user, username.encode())
    password_ok = hmac.compare_digest(given_password, password.encode())
    return bool(separator) and user_ok and password_ok


def is_cross_origin(origin: str | None, host: str | None) -> bool:
    # Pas d'en-tête Origin : curl ou script, donc pas de risque CSRF navigateur.
    if origin is None:
        return False
    return urlsplit(origin).netloc.lower() != (host or "").lower()
