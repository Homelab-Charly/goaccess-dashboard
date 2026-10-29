"""Glue between log discovery and filtering: pick files, stream matching lines."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from .filters import Filter, LineMatcher, compile_filter, line_timestamp
from .logs import LogFile, discover_logs, files_ending_after, first_line, iter_lines


@dataclass(slots=True)
class ScanStats:
    files_total: int = 0
    files_scanned: int = 0
    lines_read: int = 0
    lines_matched: int = 0
    sources: list[str] = field(default_factory=list)


def select_files(files: list[LogFile], flt: Filter) -> list[LogFile]:
    """Narrow the candidate log files using cheap, per-file information."""
    if flt.source:
        wanted = set(flt.source)
        files = [f for f in files if f.source in wanted]

    # A file cannot contain entries newer than its mtime.
    files = files_ending_after(files, flt.start_ts)

    # A file cannot contain entries older than its first line.
    if flt.end_ts is not None:
        kept = []
        for f in files:
            head = first_line(f.path)
            ts = line_timestamp(head) if head else None
            if ts is None or ts <= flt.end_ts:
                kept.append(f)
        files = kept
    return files


def iter_filtered_lines(
    files: list[LogFile],
    matcher: LineMatcher,
    stats: ScanStats | None = None,
) -> Iterator[str]:
    """Yield every line of ``files`` that passes ``matcher``."""
    for f in files:
        if stats is not None:
            stats.files_scanned += 1
        for line in iter_lines(f.path):
            if stats is not None:
                stats.lines_read += 1
            if matcher(line):
                if stats is not None:
                    stats.lines_matched += 1
                yield line


def prepare(log_dir: Path, flt: Filter, *, skip_archived: bool) -> tuple[list[LogFile], LineMatcher, ScanStats]:
    all_files = discover_logs(log_dir, skip_archived=skip_archived)
    files = select_files(all_files, flt)
    stats = ScanStats(files_total=len(all_files), sources=sorted({f.source for f in files}))
    return files, compile_filter(flt), stats
