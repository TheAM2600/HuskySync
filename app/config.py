"""Local settings. Secrets are read from the environment, never persisted here."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser().resolve() if value else default.resolve()


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_path: Path
    browser_profile_dir: Path
    browser_lock_path: Path
    blackboard_base_url: str
    timezone: str
    google_credentials_path: Path
    google_token_path: Path
    google_calendar_id: str
    imap_host: str
    imap_username: str | None = None
    imap_password: str | None = field(default=None, repr=False)
    imap_access_token: str | None = field(default=None, repr=False)

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def load_settings() -> Settings:
    data_dir = _path("HUSKYSYNC_DATA_DIR", PROJECT_ROOT / ".husky_sync")
    timezone = os.environ.get("HUSKYSYNC_TIMEZONE", "America/New_York")
    ZoneInfo(timezone)  # Fail early for invalid timezone configuration.
    return Settings(
        data_dir=data_dir,
        database_path=_path("HUSKYSYNC_DATABASE_PATH", data_dir / "husky_sync.sqlite3"),
        browser_profile_dir=_path("HUSKYSYNC_BROWSER_PROFILE_DIR", data_dir / "browser_profile"),
        browser_lock_path=_path("HUSKYSYNC_BROWSER_LOCK_PATH", data_dir / "browser.lock"),
        blackboard_base_url=os.environ.get("HUSKYSYNC_BLACKBOARD_BASE_URL", "https://huskyct.uconn.edu").rstrip("/"),
        timezone=timezone,
        google_credentials_path=_path("HUSKYSYNC_GOOGLE_CREDENTIALS_PATH", data_dir / "credentials.json"),
        google_token_path=_path("HUSKYSYNC_GOOGLE_TOKEN_PATH", data_dir / "token.json"),
        google_calendar_id=os.environ.get("HUSKYSYNC_GOOGLE_CALENDAR_ID", "primary"),
        imap_host=os.environ.get("HUSKYSYNC_IMAP_HOST", "outlook.office365.com"),
        imap_username=os.environ.get("HUSKYSYNC_IMAP_USERNAME") or None,
        imap_password=os.environ.get("HUSKYSYNC_IMAP_PASSWORD") or None,
        imap_access_token=os.environ.get("HUSKYSYNC_IMAP_ACCESS_TOKEN") or None,
    )


settings = load_settings()


def ensure_local_directories() -> None:
    """Create storage locations without replacing existing files or profiles."""
    for directory in {
        settings.data_dir,
        settings.database_path.parent,
        settings.browser_profile_dir,
        settings.browser_lock_path.parent,
        settings.google_credentials_path.parent,
        settings.google_token_path.parent,
    }:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
