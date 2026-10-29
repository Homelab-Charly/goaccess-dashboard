"""Runtime configuration, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

_TRUE = {"1", "true", "yes", "on"}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in _TRUE


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_path(name: str, default: str) -> Path:
    return Path(os.environ.get(name) or default)


def _first_existing(*candidates: str) -> Path | None:
    for c in candidates:
        if c and Path(c).is_file():
            return Path(c)
    return None


@dataclass(slots=True)
class Settings:
    # Directory holding NPM's proxy-host-*_access.log(.gz) files.
    log_dir: Path = field(default_factory=lambda: _env_path("LOG_DIR", "/opt/log"))
    # Where generated HTML reports are stored.
    cache_dir: Path = field(default_factory=lambda: _env_path("CACHE_DIR", "/cache"))
    # Seconds a generated report is considered fresh for identical filters.
    cache_ttl: int = field(default_factory=lambda: _env_int("CACHE_TTL", 60))
    # Maximum number of cached reports kept on disk.
    cache_max_reports: int = field(default_factory=lambda: _env_int("CACHE_MAX_REPORTS", 50))
    # Mirror of the xavierh image flag: ignore rotated *.gz logs.
    skip_archived_logs: bool = field(default_factory=lambda: _env_bool("SKIP_ARCHIVED_LOGS", False))
    # Path to the goaccess binary and its config file.
    goaccess_bin: str = field(default_factory=lambda: os.environ.get("GOACCESS_BIN") or "goaccess")
    goaccess_conf: Path = field(
        default_factory=lambda: _env_path(
            "GOACCESS_CONF", str(Path(__file__).resolve().parent.parent / "goaccess.conf")
        )
    )
    # Optional MaxMind database; auto-detected when not set.
    geoip_db: Path | None = field(
        default_factory=lambda: _first_existing(
            os.environ.get("GEOIP_DB", ""),
            "/geoip/GeoLite2-City.mmdb",
            "/geoip/GeoLite2-Country.mmdb",
            "/goaccess-config/GeoLite2-City.mmdb",
        )
    )
    # Timezone used to interpret "from"/"to" filter values without an offset
    # and to render datetimes in the UI.
    tz: str = field(default_factory=lambda: os.environ.get("TZ") or "UTC")
    # Hard cap on goaccess runtime per report, seconds.
    goaccess_timeout: int = field(default_factory=lambda: _env_int("GOACCESS_TIMEOUT", 600))
    # Max lines returned by /api/export (0 = unlimited).
    export_max_lines: int = field(default_factory=lambda: _env_int("EXPORT_MAX_LINES", 0))

    def ensure_dirs(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
