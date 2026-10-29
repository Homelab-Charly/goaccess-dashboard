from datetime import datetime, timezone, timedelta

import pytest

from app.filters import LINE_RE, Filter, compile_filter, filter_lines, line_timestamp, parse_log_timestamp

BRU = timezone(timedelta(hours=1))


def _match(flt: Filter, lines: list[str]) -> list[str]:
    return list(filter_lines(lines, compile_filter(flt)))


def _paths(lines: list[str]) -> list[str]:
    return [LINE_RE.match(l).group("path") for l in lines]


# --------------------------------------------------------------- parsing
def test_line_regex_parses_all_fixture_lines(sample_lines):
    for line in sample_lines:
        assert LINE_RE.match(line), line


def test_line_regex_fields(sample_lines):
    m = LINE_RE.match(sample_lines[0])
    assert m.group("ts") == "29/Oct/2026:08:00:01 +0100"
    assert m.group("status") == "200"
    assert m.group("method") == "GET"
    assert m.group("host") == "home.example.com"
    assert m.group("path") == "/"
    assert m.group("ip") == "203.0.113.7"
    assert m.group("ua").startswith("Mozilla/5.0")


def test_line_regex_tolerates_multi_upstream_and_empty_method(sample_lines):
    weird = next(l for l in sample_lines if "200, 502" in l)
    m = LINE_RE.match(weird)
    assert m.group("upstream") == "200, 502"
    assert m.group("status") == "502"
    assert m.group("method") == ""
    assert m.group("ua") == ""


def test_parse_timestamp():
    ts = parse_log_timestamp("29/Oct/2026:08:00:01 +0100")
    assert ts == datetime(2026, 10, 29, 8, 0, 1, tzinfo=BRU).timestamp()
    assert parse_log_timestamp("29/Oct/2026:08:00:01 -0500") == datetime(
        2026, 10, 29, 8, 0, 1, tzinfo=timezone(timedelta(hours=-5))
    ).timestamp()
    assert parse_log_timestamp("garbage") is None
    assert line_timestamp("not a log line") is None


# ---------------------------------------------------------------- model
def test_csv_and_repeated_values_are_merged():
    f = Filter(host=["a.com,b.com", "C.com"], status="4xx, 500", method="get")
    assert f.host == ["a.com", "b.com", "c.com"]
    assert f.status == ["4xx", "500"]
    assert f.method == ["GET"]


def test_aliases_from_to():
    f = Filter.model_validate({"from": "2026-10-29T08:00", "to": "2026-10-29T09:00"})
    assert f.start is not None and f.end is not None
    assert f.start.tzinfo is None
    loc = f.localized("Europe/Brussels")
    assert loc.start.utcoffset() == timedelta(hours=1)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"ip": "not-an-ip"},
        {"status": "abc"},
        {"status": "6xx"},
        {"path_regex": "("},
        {"from": "2026-10-29T10:00", "to": "2026-10-29T09:00"},
    ],
)
def test_invalid_filters_rejected(kwargs):
    with pytest.raises(ValueError):
        Filter.model_validate(kwargs)


def test_cache_key_is_order_independent():
    a = Filter(host=["a.com", "b.com"], status="404,4xx")
    b = Filter(host=["b.com", "a.com"], status="4xx,404")
    assert a.cache_key() == b.cache_key()
    assert a.cache_key() != Filter(host=["a.com"]).cache_key()
    assert Filter().cache_key() == Filter(ip=[], q="").cache_key()


def test_empty_filter_matches_everything(sample_lines):
    f = Filter()
    assert f.is_empty()
    assert not compile_filter(f).active
    assert _match(f, sample_lines + ["garbage line"]) == sample_lines + ["garbage line"]


# ----------------------------------------------------------- predicates
def test_time_window(sample_lines):
    f = Filter.model_validate({"from": "2026-10-29T09:00", "to": "2026-10-29T10:00:00"}).localized("Europe/Brussels")
    got = _match(f, sample_lines)
    assert _paths(got) == ["/api/v1/login", "/api/v1/login", "/web/index.html"]


def test_time_window_with_offset_string(sample_lines):
    # 08:00 UTC == 09:00 Brussels
    f = Filter.model_validate({"from": "2026-10-29T08:00:00Z", "to": "2026-10-29T08:59:59Z"})
    assert _paths(_match(f, sample_lines)) == ["/api/v1/login", "/api/v1/login"]


def test_ip_and_cidr(sample_lines):
    assert len(_match(Filter(ip="203.0.113.7"), sample_lines)) == 2
    assert len(_match(Filter(ip="203.0.113.0/24"), sample_lines)) == 4
    assert len(_match(Filter(ip="2001:db8::/32"), sample_lines)) == 1
    assert len(_match(Filter(ip_exclude="192.168.0.0/16,203.0.113.0/24"), sample_lines)) == 4


def test_host_exact_and_contains(sample_lines):
    assert len(_match(Filter(host="API.example.com"), sample_lines)) == 2
    assert len(_match(Filter(host_contains="media"), sample_lines)) == 2
    assert len(_match(Filter(host=["api.example.com", "media.example.com"]), sample_lines)) == 4


def test_status_codes_and_classes(sample_lines):
    assert len(_match(Filter(status="404"), sample_lines)) == 2
    assert len(_match(Filter(status="4xx"), sample_lines)) == 3
    assert len(_match(Filter(status="4xx,5xx"), sample_lines)) == 5
    assert len(_match(Filter(status_exclude="2xx"), sample_lines)) == 5
    assert len(_match(Filter(status="5xx", status_exclude="502"), sample_lines)) == 0


def test_method(sample_lines):
    assert len(_match(Filter(method="post"), sample_lines)) == 2
    assert len(_match(Filter(method=["GET", "POST"]), sample_lines)) == 9


def test_path_filters(sample_lines):
    assert _paths(_match(Filter(path="/api/"), sample_lines)) == ["/api/v1/login", "/api/v1/login"]
    assert len(_match(Filter(path_exclude="/health"), sample_lines)) == 9
    assert _paths(_match(Filter(path_regex=r"^/\.env$|wp-login"), sample_lines)) == ["/wp-login.php", "/.env"]


def test_user_agent(sample_lines):
    assert len(_match(Filter(ua="CURL"), sample_lines)) == 2
    assert len(_match(Filter(ua_exclude="Uptime-Kuma"), sample_lines)) == 9


def test_free_text_search(sample_lines):
    assert len(_match(Filter(q="Googlebot"), sample_lines)) == 1
    assert len(_match(Filter(q="Sent-to 192.168.0.11"), sample_lines)) == 2


def test_combined(sample_lines):
    f = Filter.model_validate(
        {"from": "2026-10-29T07:00:00Z", "ip": "198.51.100.0/24", "status": "4xx", "path": "wp-"}
    )
    assert _paths(_match(f, sample_lines)) == ["/wp-login.php"]


def test_unparsable_line_dropped_when_field_filter_active():
    assert _match(Filter(status="200"), ["garbage"]) == []
    assert _match(Filter(q="garb"), ["garbage"]) == ["garbage"]
