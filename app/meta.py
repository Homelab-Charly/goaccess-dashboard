"""Metadata about the available logs (sources, hostnames, date range), cached."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .filters import LINE_RE, line_timestamp
from .logs import LogFile, discover_logs, first_line, iter_lines, last_line


@dataclass(slots=True)
class LogMeta:
    sources: list[dict]
    hosts: list[str]
    oldest: float | None
    newest: float | None
    total_bytes: int
    scanned_at: float
    scan_duration: float
    files: int = 0
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "sources": self.sources,
            "hosts": self.hosts,
            "oldest": self.oldest,
            "newest": self.newest,
            "total_bytes": self.total_bytes,
            "files": self.files,
            "scanned_at": self.scanned_at,
            "scan_duration": self.scan_duration,
            **self.extra,
        }


def _host_of(line: str) -> str | None:
    m = LINE_RE.match(line)
    if m is None:
        return None
    host = m.group("host")
    return host or None


def _scan_hosts(files: list[LogFile]) -> set[str]:
    hosts: set[str] = set()
    for f in files:
        for line in iter_lines(f.path):
            h = _host_of(line)
            if h:
                hosts.add(h)
    return hosts


def _date_range(files: list[LogFile]) -> tuple[float | None, float | None]:
    oldest: float | None = None
    newest: float | None = None
    for f in files:
        head = first_line(f.path)
        ts = line_timestamp(head) if head else None
        if ts is not None and (oldest is None or ts < oldest):
            oldest = ts
        tail = last_line(f.path)
        ts2 = line_timestamp(tail) if tail else None
        if ts2 is None:
            ts2 = f.mtime if f.size else None
        if ts2 is not None and (newest is None or ts2 > newest):
            newest = ts2
    return oldest, newest


def scan(log_dir: Path, *, skip_archived: bool, full_host_scan: bool = True) -> LogMeta:
    started = time.time()
    files = discover_logs(log_dir, skip_archived=skip_archived)

    per_source: dict[str, dict] = {}
    for f in files:
        entry = per_source.setdefault(
            f.source, {"id": f.source, "host_num": f.host_num, "files": 0, "bytes": 0, "archived": 0}
        )
        entry["files"] += 1
        entry["bytes"] += f.size
        if f.archived:
            entry["archived"] += 1
    sources = sorted(per_source.values(), key=lambda s: s["host_num"])

    hosts = sorted(_scan_hosts(files), key=str.lower) if full_host_scan else []
    oldest, newest = _date_range(files)

    return LogMeta(
        sources=sources,
        hosts=hosts,
        oldest=oldest,
        newest=newest,
        total_bytes=sum(f.size for f in files),
        files=len(files),
        scanned_at=time.time(),
        scan_duration=round(time.time() - started, 3),
    )


class MetaCache:
    """Thread-safe TTL cache around :func:`scan`."""

    def __init__(self, log_dir: Path, *, skip_archived: bool, ttl: float = 600.0) -> None:
        self.log_dir = log_dir
        self.skip_archived = skip_archived
        self.ttl = ttl
        self._lock = threading.Lock()
        self._value: LogMeta | None = None

    def get(self, *, force: bool = False) -> LogMeta:
        with self._lock:
            if (
                not force
                and self._value is not None
                and time.time() - self._value.scanned_at < self.ttl
            ):
                return self._value
            self._value = scan(self.log_dir, skip_archived=self.skip_archived)
            return self._value
