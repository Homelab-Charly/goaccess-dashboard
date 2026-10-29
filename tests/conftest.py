import gzip
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE = FIXTURES / "sample_npm.log"

ARCHIVED_LINES = [
    '[28/Oct/2026:22:00:00 +0100] - 200 200 - GET https home.example.com "/old" [Client 203.0.113.7] [Length 10] [Gzip -] [Sent-to 192.168.0.10] "curl/8.5.0" "-"',
    '[28/Oct/2026:22:30:00 +0100] - 500 500 - GET https home.example.com "/old/crash" [Client 198.51.100.23] [Length 10] [Gzip -] [Sent-to 192.168.0.10] "curl/8.5.0" "-"',
    '[28/Oct/2026:23:59:59 +0100] - 200 200 - GET https home.example.com "/old/last" [Client 203.0.113.7] [Length 10] [Gzip -] [Sent-to 192.168.0.10] "curl/8.5.0" "-"',
]

HOST2_LINES = [
    '[29/Oct/2026:11:00:00 +0100] - 200 200 - GET https other.example.com "/" [Client 203.0.113.50] [Length 100] [Gzip -] [Sent-to 192.168.0.20] "curl/8.5.0" "-"',
    '[29/Oct/2026:11:00:01 +0100] - 304 304 - GET https other.example.com "/favicon.ico" [Client 203.0.113.50] [Length 0] [Gzip -] [Sent-to 192.168.0.20] "curl/8.5.0" "-"',
]


def _epoch(s: str) -> float:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S %z").timestamp()


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    d = tmp_path / "logs"
    d.mkdir()

    active = d / "proxy-host-1_access.log"
    shutil.copy(SAMPLE, active)

    archived = d / "proxy-host-1_access.log.1.gz"
    with gzip.open(archived, "wt", encoding="utf-8") as fh:
        fh.write("\n".join(ARCHIVED_LINES) + "\n")
    old = _epoch("2026-10-29 00:00:05 +0100")
    os.utime(archived, (old, old))

    host2 = d / "proxy-host-2_access.log"
    host2.write_text("\n".join(HOST2_LINES) + "\n")

    # noise that must be ignored
    (d / "proxy-host-1_error.log").write_text("2026/10/29 08:00:00 [error] something\n")
    (d / "fallback_access.log").write_text("not a proxy host log\n")
    (d / "default-host_access.log").write_text("not a proxy host log\n")
    return d


@pytest.fixture
def sample_lines() -> list[str]:
    return [l for l in SAMPLE.read_text().splitlines() if l]
