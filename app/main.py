"""FastAPI application: filter NPM logs and serve regenerated GoAccess reports."""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .config import settings
from .filters import Filter
from .goaccess import GoAccessError, ReportBuilder, goaccess_version
from .meta import MetaCache
from .pipeline import iter_filtered_lines, prepare

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

STATIC_DIR = Path(__file__).parent / "static"
_REPORT_ID_RE = re.compile(r"^[0-9a-f]{16}$")

@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.ensure_dirs()
    log.info(
        "log_dir=%s cache_dir=%s tz=%s geoip=%s goaccess=%s",
        settings.log_dir, settings.cache_dir, settings.tz, settings.geoip_db,
        goaccess_version(settings.goaccess_bin),
    )
    yield


app = FastAPI(
    title="GoAccess filter for Nginx Proxy Manager",
    docs_url="/api/docs",
    redoc_url=None,
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

builder = ReportBuilder(settings)
meta_cache = MetaCache(settings.log_dir, skip_archived=settings.skip_archived_logs)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def parse_filter(request: Request) -> Filter:
    """Build a :class:`Filter` from the raw query string.

    Repeated keys (``?host=a&host=b``) and comma-separated values are both
    accepted. Naive datetimes are interpreted in the configured timezone.
    """
    raw: dict[str, list[str] | str] = {}
    for key, value in request.query_params.multi_items():
        if key in ("force", "_"):
            continue
        if key in raw:
            existing = raw[key]
            raw[key] = [*existing, value] if isinstance(existing, list) else [existing, value]
        else:
            raw[key] = value
    try:
        flt = Filter.model_validate(raw)
    except ValidationError as exc:
        errors = [
            {"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"].removeprefix("Value error, ")}
            for e in exc.errors()
        ]
        raise HTTPException(status_code=422, detail=errors) from exc
    try:
        return flt.localized(settings.tz)
    except Exception as exc:  # unknown TZ name
        log.warning("invalid TZ %r: %s; using UTC", settings.tz, exc)
        return flt.localized("UTC")


def _is_true(value: str | None) -> bool:
    return (value or "").lower() in {"1", "true", "yes", "on"}


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict:
    return {"ok": True}


@app.get("/api/meta")
async def api_meta(refresh: bool = Query(False)) -> dict:
    meta = await asyncio.to_thread(meta_cache.get, force=refresh)
    data = meta.to_dict()
    data.update(
        {
            "tz": settings.tz,
            "log_dir": str(settings.log_dir),
            "skip_archived_logs": settings.skip_archived_logs,
            "cache_ttl": settings.cache_ttl,
            "geoip": settings.geoip_db is not None,
            "goaccess_version": await asyncio.to_thread(goaccess_version, settings.goaccess_bin),
            "now": datetime.now(timezone.utc).timestamp(),
        }
    )
    return data


@app.get("/api/report")
async def api_report(request: Request) -> JSONResponse:
    flt = parse_filter(request)
    force = _is_true(request.query_params.get("force"))
    try:
        report = await builder.build(flt, force=force)
    except GoAccessError as exc:
        log.error("goaccess failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    body = report.to_dict()
    body["url"] = f"/report/{report.id}?v={int(report.generated_at)}"
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


@app.get("/report/{report_id}", include_in_schema=False)
async def report_html(report_id: str) -> FileResponse:
    if not _REPORT_ID_RE.match(report_id):
        raise HTTPException(status_code=404)
    path = builder.html_path(report_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="report expired, re-apply filters")
    return FileResponse(path, media_type="text/html", headers={"Cache-Control": "no-store"})


@app.get("/api/export")
async def api_export(request: Request) -> StreamingResponse:
    """Download the raw log lines matching the filters as a text file."""
    flt = parse_filter(request)
    files, matcher, stats = await asyncio.to_thread(
        prepare, settings.log_dir, flt, skip_archived=settings.skip_archived_logs
    )
    limit = settings.export_max_lines

    def generate():
        n = 0
        for line in iter_filtered_lines(files, matcher, stats):
            yield line + "\n"
            n += 1
            if limit and n >= limit:
                break

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    headers = {
        "Content-Disposition": f'attachment; filename="npm-access-{stamp}.log"',
        "Cache-Control": "no-store",
    }
    return StreamingResponse(generate(), media_type="text/plain; charset=utf-8", headers=headers)
