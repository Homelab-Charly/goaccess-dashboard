"""Filter model and line matching for NPM "proxy" access log lines.

NPM's nginx ``log_format proxy`` is::

    [$time_local] $upstream_cache_status $upstream_status $status - $request_method
    $scheme $host "$request_uri" [Client $remote_addr] [Length $body_bytes_sent]
    [Gzip $gzip_ratio] [Sent-to $server] "$http_user_agent" "$http_referer"

Example::

    [29/Oct/2026:15:30:01 +0100] - 200 200 - GET https example.com "/index.html"
    [Client 203.0.113.7] [Length 1234] [Gzip -] [Sent-to 192.168.0.10]
    "Mozilla/5.0 ..." "https://example.com/"
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from typing import Callable, Iterable, Iterator, Sequence
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

LINE_RE = re.compile(
    r"^\[(?P<ts>[^\]]+)\] "
    r"(?P<cache>\S+) (?P<upstream>.+?) (?P<status>\d{3}) - "
    r"(?P<method>\S*) (?P<scheme>\S*) (?P<host>\S*) "
    r'"(?P<path>.*?)" '
    r"\[Client (?P<ip>[^\]]+)\] "
    r"\[Length (?P<length>[^\]]*)\] "
    r"\[Gzip (?P<gzip>[^\]]*)\] "
    r"\[Sent-to (?P<sent_to>[^\]]*)\] "
    r'"(?P<ua>.*?)" "(?P<referer>.*)"$'
)

_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}  # fmt: skip

_STATUS_CLASS_RE = re.compile(r"^([1-5])xx$", re.IGNORECASE)
_STATUS_CODE_RE = re.compile(r"^[1-5]\d{2}$")


# --------------------------------------------------------------------------- #
# Timestamp parsing
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=65536)
def parse_log_timestamp(raw: str) -> float | None:
    """Parse ``29/Oct/2026:15:30:01 +0100`` into epoch seconds.

    Memoised: consecutive log lines very often share the same second.
    """
    try:
        date_part, tz_part = raw.split(" ", 1)
        day, mon, rest = date_part.split("/", 2)
        year, hh, mm, ss = rest.split(":")
        sign = -1 if tz_part[0] == "-" else 1
        off = sign * (int(tz_part[1:3]) * 3600 + int(tz_part[3:5]) * 60)
        dt = datetime(int(year), _MONTHS[mon], int(day), int(hh), int(mm), int(ss), tzinfo=timezone.utc)
        return dt.timestamp() - off
    except (ValueError, KeyError, IndexError):
        return None


def line_timestamp(line: str) -> float | None:
    """Fast extraction of the timestamp from a raw log line."""
    if not line.startswith("["):
        return None
    end = line.find("]")
    if end < 0:
        return None
    return parse_log_timestamp(line[1:end])


# --------------------------------------------------------------------------- #
# Filter model
# --------------------------------------------------------------------------- #
def _split_csv(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    out: list[str] = []
    for item in value:  # type: ignore[union-attr]
        if item is None:
            continue
        for part in str(item).split(","):
            part = part.strip()
            if part:
                out.append(part)
    return out


class Filter(BaseModel):
    """User-supplied filter criteria. All fields optional; empty = match all."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    start: datetime | None = Field(default=None, alias="from")
    end: datetime | None = Field(default=None, alias="to")

    ip: list[str] = Field(default_factory=list, description="Include IPs or CIDRs")
    ip_exclude: list[str] = Field(default_factory=list, description="Exclude IPs or CIDRs")

    host: list[str] = Field(default_factory=list, description="Exact virtual host names")
    host_contains: str | None = None

    status: list[str] = Field(default_factory=list, description="Codes (404) or classes (4xx)")
    status_exclude: list[str] = Field(default_factory=list)

    method: list[str] = Field(default_factory=list)

    path: str | None = Field(default=None, description="Substring of request URI")
    path_exclude: str | None = None
    path_regex: str | None = None

    ua: str | None = Field(default=None, description="Substring of user agent")
    ua_exclude: str | None = None

    q: str | None = Field(default=None, description="Substring anywhere in the raw line")

    source: list[str] = Field(default_factory=list, description="Log sources, e.g. proxy-host-1")

    ignore_crawlers: bool = False

    # ---- validators -------------------------------------------------------
    @field_validator(
        "ip", "ip_exclude", "host", "status", "status_exclude", "method", "source", mode="before"
    )
    @classmethod
    def _csv_lists(cls, v: object) -> list[str]:
        return _split_csv(v)

    @field_validator("ip", "ip_exclude")
    @classmethod
    def _valid_networks(cls, v: list[str]) -> list[str]:
        for item in v:
            try:
                ipaddress.ip_network(item, strict=False)
            except ValueError as exc:
                raise ValueError(f"invalid IP or CIDR: {item!r}") from exc
        return v

    @field_validator("status", "status_exclude")
    @classmethod
    def _valid_status(cls, v: list[str]) -> list[str]:
        out = []
        for item in v:
            if _STATUS_CODE_RE.match(item):
                out.append(item)
            elif _STATUS_CLASS_RE.match(item):
                out.append(item.lower())
            else:
                raise ValueError(f"invalid status filter: {item!r} (use 404 or 4xx)")
        return out

    @field_validator("method")
    @classmethod
    def _upper_methods(cls, v: list[str]) -> list[str]:
        return [m.upper() for m in v]

    @field_validator("host")
    @classmethod
    def _lower_hosts(cls, v: list[str]) -> list[str]:
        return [h.lower() for h in v]

    @field_validator("path_regex")
    @classmethod
    def _valid_regex(cls, v: str | None) -> str | None:
        if v:
            try:
                re.compile(v)
            except re.error as exc:
                raise ValueError(f"invalid path_regex: {exc}") from exc
        return v or None

    @field_validator("host_contains", "path", "path_exclude", "ua", "ua_exclude", "q", mode="before")
    @classmethod
    def _empty_to_none(cls, v: object) -> object:
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

    @model_validator(mode="after")
    def _window_order(self) -> "Filter":
        if self.start and self.end and self.start > self.end:
            raise ValueError("'from' must be before 'to'")
        return self

    # ---- helpers ----------------------------------------------------------
    def localized(self, tz: str) -> "Filter":
        """Return a copy where naive ``start``/``end`` are interpreted in ``tz``."""
        zone = ZoneInfo(tz)
        update: dict[str, datetime | None] = {}
        for name in ("start", "end"):
            value: datetime | None = getattr(self, name)
            if value is not None and value.tzinfo is None:
                update[name] = value.replace(tzinfo=zone)
        return self.model_copy(update=update) if update else self

    @property
    def start_ts(self) -> float | None:
        return self.start.timestamp() if self.start else None

    @property
    def end_ts(self) -> float | None:
        return self.end.timestamp() if self.end else None

    def has_line_predicates(self) -> bool:
        """True when any criterion requires inspecting individual lines."""
        return any(
            [
                self.start, self.end, self.ip, self.ip_exclude, self.host, self.host_contains,
                self.status, self.status_exclude, self.method, self.path, self.path_exclude,
                self.path_regex, self.ua, self.ua_exclude, self.q,
            ]
        )  # fmt: skip

    def is_empty(self) -> bool:
        return not self.has_line_predicates() and not self.source and not self.ignore_crawlers

    def normalized(self) -> dict:
        """Canonical, JSON-serialisable representation used for caching."""
        data = self.model_dump(mode="json", by_alias=True, exclude_none=True)
        for key in list(data):
            value = data[key]
            if isinstance(value, list):
                if not value:
                    del data[key]
                else:
                    data[key] = sorted(set(value))
            elif value is False:
                del data[key]
        return dict(sorted(data.items()))

    def cache_key(self, *extra: object) -> str:
        payload = json.dumps([self.normalized(), *extra], sort_keys=True, default=str)
        return hashlib.sha1(payload.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=65536)
def _parse_ip(raw: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(raw)
    except ValueError:
        return None


def _status_set(items: Sequence[str]) -> frozenset[str]:
    codes: set[str] = set()
    for item in items:
        m = _STATUS_CLASS_RE.match(item)
        if m:
            hundred = int(m.group(1)) * 100
            codes.update(str(c) for c in range(hundred, hundred + 100))
        else:
            codes.add(item)
    return frozenset(codes)


def _networks(items: Sequence[str]) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    return [ipaddress.ip_network(i, strict=False) for i in items]


def _ip_in(nets: list, raw_ip: str) -> bool:
    addr = _parse_ip(raw_ip)
    if addr is None:
        return False
    for net in nets:
        if addr.version == net.version and addr in net:
            return True
    return False


Predicate = Callable[[re.Match[str]], bool]


@dataclass(slots=True)
class LineMatcher:
    """Callable deciding whether a raw log line passes a :class:`Filter`."""

    start_ts: float | None
    end_ts: float | None
    q: str | None
    predicates: tuple[Predicate, ...]

    @property
    def active(self) -> bool:
        return bool(self.start_ts is not None or self.end_ts is not None or self.q or self.predicates)

    def __call__(self, line: str) -> bool:
        if not self.active:
            return True
        if self.q is not None and self.q not in line:
            return False
        if self.start_ts is not None or self.end_ts is not None:
            ts = line_timestamp(line)
            if ts is None:
                return False
            if self.start_ts is not None and ts < self.start_ts:
                return False
            if self.end_ts is not None and ts > self.end_ts:
                return False
        if not self.predicates:
            return True
        m = LINE_RE.match(line)
        if m is None:
            return False
        for pred in self.predicates:
            if not pred(m):
                return False
        return True


def compile_filter(flt: Filter) -> LineMatcher:
    preds: list[Predicate] = []

    if flt.ip:
        nets = _networks(flt.ip)
        preds.append(lambda m, nets=nets: _ip_in(nets, m.group("ip")))
    if flt.ip_exclude:
        nets = _networks(flt.ip_exclude)
        preds.append(lambda m, nets=nets: not _ip_in(nets, m.group("ip")))

    if flt.host:
        hosts = frozenset(flt.host)
        preds.append(lambda m, hosts=hosts: m.group("host").lower() in hosts)
    if flt.host_contains:
        needle = flt.host_contains.lower()
        preds.append(lambda m, needle=needle: needle in m.group("host").lower())

    if flt.status:
        allowed = _status_set(flt.status)
        preds.append(lambda m, allowed=allowed: m.group("status") in allowed)
    if flt.status_exclude:
        denied = _status_set(flt.status_exclude)
        preds.append(lambda m, denied=denied: m.group("status") not in denied)

    if flt.method:
        methods = frozenset(flt.method)
        preds.append(lambda m, methods=methods: m.group("method").upper() in methods)

    if flt.path:
        needle = flt.path
        preds.append(lambda m, needle=needle: needle in m.group("path"))
    if flt.path_exclude:
        needle = flt.path_exclude
        preds.append(lambda m, needle=needle: needle not in m.group("path"))
    if flt.path_regex:
        rx = re.compile(flt.path_regex)
        preds.append(lambda m, rx=rx: rx.search(m.group("path")) is not None)

    if flt.ua:
        needle = flt.ua.lower()
        preds.append(lambda m, needle=needle: needle in m.group("ua").lower())
    if flt.ua_exclude:
        needle = flt.ua_exclude.lower()
        preds.append(lambda m, needle=needle: needle not in m.group("ua").lower())

    return LineMatcher(
        start_ts=flt.start_ts,
        end_ts=flt.end_ts,
        q=flt.q or None,
        predicates=tuple(preds),
    )


def filter_lines(lines: Iterable[str], matcher: LineMatcher) -> Iterator[str]:
    if not matcher.active:
        yield from lines
        return
    for line in lines:
        if matcher(line):
            yield line
