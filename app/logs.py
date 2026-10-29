"""Discovery and streaming of Nginx Proxy Manager access logs.

NPM writes one access log per proxy host:

    proxy-host-<N>_access.log          active log
    proxy-host-<N>_access.log.1        rotated, not yet compressed (delaycompress)
    proxy-host-<N>_access.log.2.gz     rotated and gzipped

Every file is exposed as a :class:`LogFile`; files sharing the same ``<N>``
belong to the same *source* (``proxy-host-<N>``), which is what the UI lets
the user include or exclude.
"""

from __future__ import annotations

import gzip
import io
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

_LOG_RE = re.compile(
    r"^(?P<source>proxy-host-(?P<num>\d+))_access\.log(?P<rot>(?:\.\d+)?)(?P<gz>\.gz)?$"
)

_READ_ENCODING = "utf-8"
_READ_ERRORS = "replace"


@dataclass(frozen=True, slots=True)
class LogFile:
    path: Path
    source: str  # e.g. "proxy-host-3"
    host_num: int
    rotation: int  # 0 for the active log, N for ".N" / ".N.gz"
    compressed: bool
    mtime: float
    size: int

    @property
    def archived(self) -> bool:
        return self.rotation > 0 or self.compressed


def discover_logs(log_dir: Path, *, skip_archived: bool = False) -> list[LogFile]:
    """Return all NPM proxy access logs in ``log_dir``.

    Sorted by source, then oldest rotation first (highest ``.N`` first), so the
    active log is the last file of each source.
    """
    if not log_dir.is_dir():
        return []

    files: list[LogFile] = []
    with os.scandir(log_dir) as it:
        for entry in it:
            if not entry.is_file(follow_symlinks=True):
                continue
            m = _LOG_RE.match(entry.name)
            if not m:
                continue
            compressed = m.group("gz") is not None
            rot = m.group("rot")
            rotation = int(rot[1:]) if rot else 0
            lf_archived = rotation > 0 or compressed
            if skip_archived and lf_archived:
                continue
            st = entry.stat(follow_symlinks=True)
            files.append(
                LogFile(
                    path=Path(entry.path),
                    source=m.group("source"),
                    host_num=int(m.group("num")),
                    rotation=rotation,
                    compressed=compressed,
                    mtime=st.st_mtime,
                    size=st.st_size,
                )
            )

    files.sort(key=lambda f: (f.host_num, -f.rotation, f.compressed))
    return files


def sources(files: Iterable[LogFile]) -> list[str]:
    """Distinct source ids in numeric order."""
    seen: dict[str, int] = {}
    for f in files:
        seen.setdefault(f.source, f.host_num)
    return [s for s, _ in sorted(seen.items(), key=lambda kv: kv[1])]


def open_text(path: Path) -> io.TextIOBase:
    """Open a (possibly gzipped) log file for text reading."""
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding=_READ_ENCODING, errors=_READ_ERRORS)  # type: ignore[return-value]
    return open(path, "rt", encoding=_READ_ENCODING, errors=_READ_ERRORS, buffering=1 << 20)


def iter_lines(path: Path) -> Iterator[str]:
    """Yield the lines of a log file without trailing newlines."""
    with open_text(path) as fh:
        for line in fh:
            if line.endswith("\n"):
                line = line[:-1]
            if line:
                yield line


def first_line(path: Path) -> str | None:
    """Return the first non-empty line of a log file, or None if empty."""
    try:
        with open_text(path) as fh:
            for line in fh:
                line = line.rstrip("\n")
                if line:
                    return line
    except OSError:
        return None
    return None


def last_line(path: Path, *, chunk: int = 64 * 1024) -> str | None:
    """Return the last non-empty line of an uncompressed log file.

    Reads from the end of the file. Returns None for gzipped files (would
    require a full decompression) or when the file is empty.
    """
    if path.suffix == ".gz":
        return None
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            end = fh.tell()
            buf = b""
            pos = end
            while pos > 0:
                step = min(chunk, pos)
                pos -= step
                fh.seek(pos)
                buf = fh.read(step) + buf
                stripped = buf.rstrip(b"\n")
                if b"\n" in stripped:
                    return stripped.rsplit(b"\n", 1)[1].decode(_READ_ENCODING, _READ_ERRORS)
            stripped = buf.rstrip(b"\n")
            return stripped.decode(_READ_ENCODING, _READ_ERRORS) if stripped else None
    except OSError:
        return None


def files_ending_after(files: Iterable[LogFile], ts: float | None) -> list[LogFile]:
    """Drop files whose modification time is before ``ts`` (epoch seconds).

    A file cannot contain entries newer than its mtime, so when the requested
    time window starts after the file was last written it cannot match.
    A small slack is kept to tolerate clock drift between hosts.
    """
    if ts is None:
        return list(files)
    slack = 120.0
    return [f for f in files if f.mtime + slack >= ts]
