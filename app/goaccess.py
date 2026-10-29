"""Run GoAccess over filtered log lines and cache the resulting HTML report."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import Settings
from .filters import Filter
from .pipeline import ScanStats, iter_filtered_lines, prepare

log = logging.getLogger(__name__)


class GoAccessError(RuntimeError):
    pass


@dataclass(slots=True)
class Report:
    id: str
    generated_at: float
    duration: float
    matched_lines: int
    lines_read: int
    files_scanned: int
    files_total: int
    sources: list[str]
    filter: dict
    cached: bool = False
    empty: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _empty_report_html(flt: Filter) -> str:
    desc = html.escape(json.dumps(flt.normalized(), indent=2)) if not flt.is_empty() else "(no filters)"
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>No data</title>"
        "<style>body{font-family:system-ui,sans-serif;color:#333;background:#fff;"
        "display:flex;align-items:center;justify-content:center;height:100vh;margin:0}"
        ".box{text-align:center;max-width:40em}pre{text-align:left;background:#f4f4f4;"
        "padding:1em;border-radius:6px;font-size:12px}</style></head><body><div class='box'>"
        "<h2>No requests match the current filters</h2>"
        f"<pre>{desc}</pre></div></body></html>"
    )


def goaccess_version(binary: str) -> str | None:
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    first = (out.stdout or out.stderr).strip().splitlines()
    return first[0] if first else None


class ReportBuilder:
    """Builds and caches GoAccess HTML reports, one GoAccess process at a time."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = asyncio.Lock()

    # ---- cache helpers ----------------------------------------------------
    def html_path(self, report_id: str) -> Path:
        return self.settings.cache_dir / f"{report_id}.html"

    def meta_path(self, report_id: str) -> Path:
        return self.settings.cache_dir / f"{report_id}.json"

    def _load_meta(self, report_id: str) -> Report | None:
        try:
            data = json.loads(self.meta_path(report_id).read_text())
            return Report(**data)
        except (OSError, ValueError, TypeError):
            return None

    def _cached(self, report_id: str) -> Report | None:
        meta = self._load_meta(report_id)
        if meta is None or not self.html_path(report_id).is_file():
            return None
        if time.time() - meta.generated_at > self.settings.cache_ttl:
            return None
        meta.cached = True
        return meta

    def _evict(self) -> None:
        htmls = sorted(self.settings.cache_dir.glob("*.html"), key=lambda p: p.stat().st_mtime)
        excess = len(htmls) - self.settings.cache_max_reports
        for p in htmls[: max(0, excess)]:
            p.unlink(missing_ok=True)
            p.with_suffix(".json").unlink(missing_ok=True)

    def report_id(self, flt: Filter) -> str:
        return flt.cache_key(self.settings.skip_archived_logs, str(self.settings.log_dir))

    # ---- public API -------------------------------------------------------
    async def build(self, flt: Filter, *, force: bool = False) -> Report:
        report_id = self.report_id(flt)
        if not force:
            hit = self._cached(report_id)
            if hit:
                return hit
        async with self._lock:
            if not force:
                hit = self._cached(report_id)
                if hit:
                    return hit
            report = await asyncio.to_thread(self._build_sync, flt, report_id)
            self._evict()
            return report

    # ---- implementation ---------------------------------------------------
    def _command(self, output: Path, flt: Filter) -> list[str]:
        s = self.settings
        cmd = [
            s.goaccess_bin,
            "-",
            "--no-global-config",
            f"--config-file={s.goaccess_conf}",
            f"--output={output}",
            "--no-progress",
            f"--html-report-title={self._title(flt)}",
        ]
        if s.geoip_db:
            cmd.append(f"--geoip-database={s.geoip_db}")
        if flt.ignore_crawlers:
            cmd.append("--ignore-crawlers")
        return cmd

    @staticmethod
    def _title(flt: Filter) -> str:
        if flt.is_empty():
            return "NPM - all requests"
        parts = []
        if flt.start:
            parts.append(f"from {flt.start:%Y-%m-%d %H:%M}")
        if flt.end:
            parts.append(f"to {flt.end:%Y-%m-%d %H:%M}")
        if flt.host:
            parts.append("host " + ",".join(flt.host))
        if flt.ip:
            parts.append("ip " + ",".join(flt.ip))
        if flt.status:
            parts.append("status " + ",".join(flt.status))
        return "NPM - " + " ".join(parts) if parts else "NPM - filtered"

    def _build_sync(self, flt: Filter, report_id: str) -> Report:
        s = self.settings
        s.ensure_dirs()
        started = time.time()
        files, matcher, stats = prepare(s.log_dir, flt, skip_archived=s.skip_archived_logs)

        final_html = self.html_path(report_id)
        fd, tmp_name = tempfile.mkstemp(prefix=".report-", suffix=".html", dir=s.cache_dir)
        os.close(fd)
        tmp_html = Path(tmp_name)
        try:
            empty = self._run_goaccess(files, matcher, stats, tmp_html, flt)
            if empty:
                tmp_html.write_text(_empty_report_html(flt), encoding="utf-8")
            os.replace(tmp_html, final_html)
        finally:
            tmp_html.unlink(missing_ok=True)

        report = Report(
            id=report_id,
            generated_at=time.time(),
            duration=round(time.time() - started, 3),
            matched_lines=stats.lines_matched,
            lines_read=stats.lines_read,
            files_scanned=stats.files_scanned,
            files_total=stats.files_total,
            sources=stats.sources,
            filter=flt.normalized(),
            empty=empty,
        )
        self.meta_path(report_id).write_text(json.dumps(report.to_dict()))
        log.info(
            "report %s: %d/%d lines from %d files in %.2fs",
            report_id, stats.lines_matched, stats.lines_read, stats.files_scanned, report.duration,
        )
        return report

    def _run_goaccess(self, files, matcher, stats: ScanStats, output: Path, flt: Filter) -> bool:
        """Stream filtered lines into goaccess. Returns True when nothing matched."""
        if shutil.which(self.settings.goaccess_bin) is None:
            raise GoAccessError(f"goaccess binary not found: {self.settings.goaccess_bin!r}")

        cmd = self._command(output, flt)
        with tempfile.TemporaryFile(mode="w+b") as stderr:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=stderr,
                env={**os.environ, "TZ": self.settings.tz, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
            )
            assert proc.stdin is not None
            matched_any = False
            try:
                enc = proc.stdin
                buf: list[bytes] = []
                size = 0
                for line in iter_filtered_lines(files, matcher, stats):
                    matched_any = True
                    data = line.encode("utf-8", "replace") + b"\n"
                    buf.append(data)
                    size += len(data)
                    if size >= 1 << 20:
                        enc.write(b"".join(buf))
                        buf.clear()
                        size = 0
                if buf:
                    enc.write(b"".join(buf))
            except BrokenPipeError:
                pass
            finally:
                try:
                    proc.stdin.close()
                except OSError:
                    pass

            try:
                rc = proc.wait(timeout=self.settings.goaccess_timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                raise GoAccessError("goaccess timed out") from None

            stderr.seek(0)
            err = stderr.read().decode("utf-8", "replace").strip()

        if not matched_any:
            return True
        if rc != 0 or not output.is_file() or output.stat().st_size == 0:
            if "Nothing valid to process" in err:
                return True
            raise GoAccessError(f"goaccess exited with {rc}: {err[-2000:]}")
        return False
