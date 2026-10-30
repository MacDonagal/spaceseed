from dataclasses import dataclass
import os


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    app_name: str = os.getenv("APP_NAME", "SpaceSeed")
    database_url: str = os.getenv("DATABASE_URL", "sqlite:////app/data/spaceseed.db")
    data_dir: str = os.getenv("DATA_DIR", "/app/data")
    scan_interval_minutes: int = int(os.getenv("SCAN_INTERVAL_MINUTES", "5"))
    stale_after_minutes: int = int(os.getenv("STALE_AFTER_MINUTES", "30"))
    scan_history_days: int = int(os.getenv("SCAN_HISTORY_DAYS", "30"))
    torznab_url: str = os.getenv("TORZNAB_URL", "")
    torznab_api_key: str = os.getenv("TORZNAB_API_KEY", "")
    torznab_limit: int = int(os.getenv("TORZNAB_LIMIT", "100"))
    torznab_categories: str = os.getenv("TORZNAB_CATEGORIES", "")
    request_timeout_seconds: int = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "30"))
    min_leechers: int = int(os.getenv("MIN_LEECHERS", "1"))
    max_seeders: int = int(os.getenv("MAX_SEEDERS", "9999"))
    freeleech_only: bool = env_bool("FREELEECH_ONLY", False)

    auto_grab_enabled: bool = env_bool("AUTO_GRAB_ENABLED", False)
    auto_grab_min_score: float = float(os.getenv("AUTO_GRAB_MIN_SCORE", "150"))
    auto_grab_min_seeders: int = int(os.getenv("AUTO_GRAB_MIN_SEEDERS", "1"))
    auto_grab_freeleech_only: bool = env_bool("AUTO_GRAB_FREELEECH_ONLY", True)
    auto_grab_max_size_gb: float = float(os.getenv("AUTO_GRAB_MAX_SIZE_GB", "15"))
    auto_grab_max_per_scan: int = int(os.getenv("AUTO_GRAB_MAX_PER_SCAN", "1"))
    auto_grab_max_active: int = int(os.getenv("AUTO_GRAB_MAX_ACTIVE", "2"))
    auto_grab_min_free_space_gb: float = float(os.getenv("AUTO_GRAB_MIN_FREE_SPACE_GB", "50"))

    # Le contrôle du ratio est actif dès qu'une clé est définie.
    c411_api_key: str = os.getenv("C411_API_KEY", "")
    c411_api_url: str = os.getenv("C411_API_URL", "https://c411.org/api/user/profile")
    c411_min_margin_gb: float = float(os.getenv("C411_MIN_MARGIN_GB", "20"))

    # Les alertes sont actives dès que le token et le chat sont définis.
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
    telegram_min_score: float = float(os.getenv("TELEGRAM_MIN_SCORE", "150"))
    telegram_freeleech_only: bool = env_bool("TELEGRAM_FREELEECH_ONLY", False)
    telegram_max_per_scan: int = int(os.getenv("TELEGRAM_MAX_PER_SCAN", "5"))
    telegram_min_interval_minutes: int = int(os.getenv("TELEGRAM_MIN_INTERVAL_MINUTES", "60"))

    # Interface : l'authentification est active dès qu'un mot de passe est défini.
    spaceseed_username: str = os.getenv("SPACESEED_USERNAME", "admin")
    spaceseed_password: str = os.getenv("SPACESEED_PASSWORD", "")

    qbittorrent_url: str = os.getenv("QBITTORRENT_URL", "")
    qbittorrent_username: str = os.getenv("QBITTORRENT_USERNAME", "")
    qbittorrent_password: str = os.getenv("QBITTORRENT_PASSWORD", "")
    qbittorrent_category: str = os.getenv("QBITTORRENT_CATEGORY", "spaceseed")
    qbittorrent_save_path: str = os.getenv("QBITTORRENT_SAVE_PATH", "")
    qbittorrent_paused: bool = env_bool("QBITTORRENT_PAUSED", False)


settings = Settings()
