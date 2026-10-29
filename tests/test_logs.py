from datetime import datetime, timezone, timedelta
from pathlib import Path

from app.filters import Filter, compile_filter
from app.logs import discover_logs, files_ending_after, first_line, iter_lines, last_line, sources
from app.meta import scan
from app.pipeline import ScanStats, iter_filtered_lines, prepare, select_files

BRU = timezone(timedelta(hours=1))


def test_discover_logs(log_dir: Path):
    files = discover_logs(log_dir)
    names = [f.path.name for f in files]
    assert names == [
        "proxy-host-1_access.log.1.gz",
        "proxy-host-1_access.log",
        "proxy-host-2_access.log",
    ]
    gz, active, host2 = files
    assert gz.archived and gz.compressed and gz.rotation == 1 and gz.source == "proxy-host-1"
    assert not active.archived and active.rotation == 0
    assert host2.host_num == 2
    assert sources(files) == ["proxy-host-1", "proxy-host-2"]


def test_discover_skip_archived(log_dir: Path):
    files = discover_logs(log_dir, skip_archived=True)
    assert [f.path.name for f in files] == ["proxy-host-1_access.log", "proxy-host-2_access.log"]


def test_discover_missing_dir(tmp_path: Path):
    assert discover_logs(tmp_path / "nope") == []


def test_iter_lines_transparent_gzip(log_dir: Path):
    gz_lines = list(iter_lines(log_dir / "proxy-host-1_access.log.1.gz"))
    assert len(gz_lines) == 3 and gz_lines[0].startswith("[28/Oct/2026:22:00:00")
    plain = list(iter_lines(log_dir / "proxy-host-1_access.log"))
    assert len(plain) == 10


def test_first_and_last_line(log_dir: Path):
    active = log_dir / "proxy-host-1_access.log"
    assert first_line(active).startswith("[29/Oct/2026:08:00:01")
    assert last_line(active).startswith("[29/Oct/2026:12:00:00")
    assert "Googlebot" in last_line(active)
    assert last_line(log_dir / "proxy-host-1_access.log.1.gz") is None
    empty = log_dir / "proxy-host-9_access.log"
    empty.write_text("")
    assert first_line(empty) is None and last_line(empty) is None


def test_files_ending_after_uses_mtime(log_dir: Path):
    files = discover_logs(log_dir)
    cutoff = datetime(2026, 10, 29, 6, 0, tzinfo=BRU).timestamp()
    kept = files_ending_after(files, cutoff)
    assert [f.path.name for f in kept] == ["proxy-host-1_access.log", "proxy-host-2_access.log"]
    assert files_ending_after(files, None) == files


def test_select_files_by_source_and_window(log_dir: Path):
    files = discover_logs(log_dir)
    assert [f.source for f in select_files(files, Filter(source="proxy-host-2"))] == ["proxy-host-2"]

    # window ending before the active logs start -> only the archive remains
    flt = Filter.model_validate({"to": "2026-10-29T01:00"}).localized("Europe/Brussels")
    assert [f.path.name for f in select_files(files, flt)] == ["proxy-host-1_access.log.1.gz"]

    # window starting after the archive's mtime -> archive dropped
    flt = Filter.model_validate({"from": "2026-10-29T07:00"}).localized("Europe/Brussels")
    assert "proxy-host-1_access.log.1.gz" not in [f.path.name for f in select_files(files, flt)]


def test_iter_filtered_lines_and_stats(log_dir: Path):
    files = discover_logs(log_dir)
    stats = ScanStats()
    out = list(iter_filtered_lines(files, compile_filter(Filter(status="5xx")), stats))
    assert len(out) == 3  # 500 in archive, 502 x2 in active
    assert stats.files_scanned == 3
    assert stats.lines_read == 15
    assert stats.lines_matched == 3


def test_prepare_end_to_end(log_dir: Path):
    flt = Filter(host="other.example.com")
    files, matcher, stats = prepare(log_dir, flt, skip_archived=False)
    lines = list(iter_filtered_lines(files, matcher, stats))
    assert len(lines) == 2
    assert stats.files_total == 3
    assert stats.sources == ["proxy-host-1", "proxy-host-2"]


def test_meta_scan(log_dir: Path):
    meta = scan(log_dir, skip_archived=False)
    assert meta.files == 3
    assert [s["id"] for s in meta.sources] == ["proxy-host-1", "proxy-host-2"]
    assert meta.sources[0]["files"] == 2 and meta.sources[0]["archived"] == 1
    assert meta.hosts == ["api.example.com", "home.example.com", "media.example.com", "other.example.com"]
    assert meta.oldest == datetime(2026, 10, 28, 22, 0, tzinfo=BRU).timestamp()
    assert meta.newest == datetime(2026, 10, 29, 12, 0, tzinfo=BRU).timestamp()
    assert meta.total_bytes > 0
