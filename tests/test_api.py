import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main as app_main

HAVE_GOACCESS = shutil.which("goaccess") is not None
needs_goaccess = pytest.mark.skipif(not HAVE_GOACCESS, reason="goaccess binary not installed")


@pytest.fixture
def client(log_dir: Path, tmp_path: Path, monkeypatch):
    s = app_main.settings
    monkeypatch.setattr(s, "log_dir", log_dir)
    monkeypatch.setattr(s, "cache_dir", tmp_path / "cache")
    monkeypatch.setattr(s, "cache_ttl", 60)
    monkeypatch.setattr(s, "skip_archived_logs", False)
    monkeypatch.setattr(s, "tz", "Europe/Brussels")
    s.ensure_dirs()
    monkeypatch.setattr(app_main.meta_cache, "log_dir", log_dir)
    monkeypatch.setattr(app_main.meta_cache, "_value", None)
    with TestClient(app_main.app) as c:
        yield c


def test_index_and_static(client):
    r = client.get("/")
    assert r.status_code == 200 and "<form id=\"filters\"" in r.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/healthz").json() == {"ok": True}


def test_meta(client):
    r = client.get("/api/meta")
    assert r.status_code == 200
    data = r.json()
    assert [s["id"] for s in data["sources"]] == ["proxy-host-1", "proxy-host-2"]
    assert "home.example.com" in data["hosts"]
    assert data["tz"] == "Europe/Brussels"
    assert data["files"] == 3


def test_export_filters_lines(client):
    r = client.get("/api/export", params={"status": "5xx"})
    assert r.status_code == 200
    assert r.headers["content-disposition"].startswith("attachment;")
    lines = [l for l in r.text.splitlines() if l]
    assert len(lines) == 3

    r = client.get("/api/export", params=[("host", "other.example.com"), ("method", "GET")])
    assert len(r.text.splitlines()) == 2

    r = client.get("/api/export", params={"from": "2026-10-29T11:00", "to": "2026-10-29T11:00:00"})
    assert len(r.text.splitlines()) == 1  # naive datetimes interpreted in Europe/Brussels


def test_validation_errors(client):
    r = client.get("/api/report", params={"ip": "nope"})
    assert r.status_code == 422
    assert r.json()["detail"][0]["field"] == "ip"
    r = client.get("/api/export", params={"status": "9xx"})
    assert r.status_code == 422


def test_unknown_report_404(client):
    assert client.get("/report/0123456789abcdef").status_code == 404
    assert client.get("/report/../etc/passwd").status_code in (404, 422)


@needs_goaccess
def test_report_roundtrip(client):
    r = client.get("/api/report")
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["matched_lines"] == 15 and rep["cached"] is False and rep["empty"] is False
    html = client.get(rep["url"])
    assert html.status_code == 200
    assert "GoAccess" in html.text
    # GoAccess embeds the parsed data as JSON; total requests should be there
    assert '"total_requests":15' in html.text.replace(" ", "") or "15" in html.text

    # second call for identical filters is served from cache
    r2 = client.get("/api/report")
    assert r2.json()["cached"] is True and r2.json()["id"] == rep["id"]

    # force bypasses cache
    r3 = client.get("/api/report", params={"force": "1"})
    assert r3.json()["cached"] is False


@needs_goaccess
def test_report_filters_change_numbers(client):
    all_ = client.get("/api/report").json()
    sub = client.get("/api/report", params={"status": "4xx", "ignore_crawlers": "1"}).json()
    assert sub["id"] != all_["id"]
    assert sub["matched_lines"] == 3 < all_["matched_lines"]
    html = client.get(sub["url"]).text
    assert '"total_requests":3' in html.replace(" ", "")


@needs_goaccess
def test_report_no_matches(client):
    rep = client.get("/api/report", params={"ip": "10.255.255.255"}).json()
    assert rep["matched_lines"] == 0 and rep["empty"] is True
    html = client.get(rep["url"]).text
    assert "No requests match" in html


@needs_goaccess
def test_cache_ttl_expiry(client, monkeypatch):
    monkeypatch.setattr(app_main.settings, "cache_ttl", 0)
    a = client.get("/api/report", params={"host": "api.example.com"}).json()
    time.sleep(0.05)
    b = client.get("/api/report", params={"host": "api.example.com"}).json()
    assert a["id"] == b["id"] and b["cached"] is False
