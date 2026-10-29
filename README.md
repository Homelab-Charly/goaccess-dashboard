# goaccess-dashboard

A small web app that puts a **filter bar on top of the GoAccess dashboard** for
[Nginx Proxy Manager](https://nginxproxymanager.com/) logs.

GoAccess itself cannot filter. This app filters the NPM access-log lines in
Python (time window, IP/CIDR, host, status, method, path, user agent, free
text, proxy host) and pipes only the matching lines into `goaccess`, which
regenerates the familiar HTML report on demand. The report is shown in an
iframe under the filter bar; filter state lives in the URL so views are
bookmarkable.

```
Browser ──GET /api/report?from&ip&host&status…──▶ FastAPI
                                                   │ discover proxy-host-*_access.log(.gz)
                                                   │ regex-parse lines, apply predicates
                                                   ▼
                                              goaccess - (stdin) ──▶ /cache/<hash>.html
Browser ◀──────────────── iframe /report/<hash> ───┘
```

## Features

- Time presets (1h, 6h, 24h, 7d, 30d, all) or a custom from/to window
- Include / exclude IPs and CIDR ranges (IPv4 and IPv6)
- Virtual host multi-select (discovered from the logs), or substring match
- Status codes (`404`) and classes (`4xx`), include and exclude
- HTTP method, path substring / exclude / regex, user agent include / exclude
- Free-text search over the raw line, per-proxy-host selection, ignore crawlers
- Reads active logs and rotated `.1`, `.gz` archives; skips files that cannot
  overlap the requested window
- Reports cached per filter set (default 60 s) and generated one at a time
- Export the matching raw log lines as a `.log` file
- GeoIP panels work out of the box (GeoLite2 database copied from the
  `xavierh/goaccess-for-nginxproxymanager` image at build time)

## Run next to your existing stack

Clone this repository next to your compose file and add the service from
[`docker-compose.example.yml`](docker-compose.example.yml):

```yaml
  goaccess-filter:
    build: ./goaccess-dashboard
    container_name: goaccess-filter
    restart: unless-stopped
    environment:
      - TZ=Europe/Brussels
      - SKIP_ARCHIVED_LOGS=False
    ports:
      - '192.168.0.3:7881:8000'   # LAN only, like goaccess
    volumes:
      - ./data/logs:/opt/log:ro
      - goaccess-filter-cache:/cache

volumes:
  goaccess-filter-cache:
```

Then `docker compose up -d --build goaccess-filter` and open
`http://192.168.0.3:7881/`. The existing `goaccess` container keeps working
unchanged; this is an additional service.

There is no authentication. Bind the port to your LAN address (as above) or
put it behind NPM with an access list if you expose it further.

## Configuration

| Variable             | Default                    | Description                                             |
|----------------------|----------------------------|---------------------------------------------------------|
| `LOG_DIR`            | `/opt/log`                 | Directory with `proxy-host-*_access.log*`               |
| `CACHE_DIR`          | `/cache`                   | Where generated reports are written                     |
| `CACHE_TTL`          | `60`                       | Seconds a report is reused for identical filters        |
| `CACHE_MAX_REPORTS`  | `50`                       | Max reports kept on disk                                |
| `SKIP_ARCHIVED_LOGS` | `False`                    | Ignore rotated `.N` / `.gz` logs                        |
| `TZ`                 | `UTC`                      | Timezone for naive `from`/`to` values and report labels |
| `GEOIP_DB`           | auto (`/geoip/*.mmdb`)     | MaxMind database for the geo panels                     |
| `GOACCESS_TIMEOUT`   | `600`                      | Max seconds per `goaccess` run                          |
| `EXPORT_MAX_LINES`   | `0` (unlimited)            | Cap on lines returned by `/api/export`                  |

To use your own MaxMind database, mount it at `/geoip/GeoLite2-City.mmdb`
(or point `GEOIP_DB` at it). To build without GeoIP, remove the
`COPY --from=geoip …` line from the `Dockerfile`.

## API

All filter parameters are query parameters; lists accept repeated keys or
comma-separated values. Naive datetimes are interpreted in `TZ`; ISO strings
with an offset (`2026-10-29T08:00:00Z`) are used as-is.

| Endpoint             | Description                                                            |
|----------------------|------------------------------------------------------------------------|
| `GET /`              | The UI                                                                 |
| `GET /api/meta`      | Discovered proxy hosts, hostnames, log date range (`?refresh=1` rescans) |
| `GET /api/report`    | Build (or reuse) a report; returns `{id, url, matched_lines, …}`; `?force=1` bypasses the cache |
| `GET /report/{id}`   | The generated GoAccess HTML                                            |
| `GET /api/export`    | Download matching raw log lines                                        |
| `GET /api/docs`      | OpenAPI UI                                                             |

Filter parameters: `from`, `to`, `ip`, `ip_exclude`, `host`, `host_contains`,
`status`, `status_exclude`, `method`, `path`, `path_exclude`, `path_regex`,
`ua`, `ua_exclude`, `q`, `source`, `ignore_crawlers`.

Example:

```
/api/report?from=2026-10-28T00:00:00Z&status=4xx,5xx&ip_exclude=192.168.0.0/16&host=home.example.com
```

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest                      # goaccess-dependent tests are skipped if goaccess is not installed
LOG_DIR=/path/to/npm/logs CACHE_DIR=/tmp/ga-cache TZ=Europe/Brussels \
  uvicorn app.main:app --reload
```

To run the full suite including the GoAccess integration tests inside the
image:

```bash
docker build -t goaccess-filter:dev .
docker run --rm -v "$PWD/tests:/srv/tests:ro" goaccess-filter:dev \
  sh -c "pip install -q pytest httpx && cd /srv && pytest -q"
```

## Layout

- `app/logs.py`: find `proxy-host-N_access.log`, `.N`, `.gz`; stream lines; cheap per-file skipping
- `app/filters.py`: `Filter` model, NPM line regex, predicates, cache key
- `app/pipeline.py`: file selection and filtered line iterator
- `app/goaccess.py`: runs `goaccess` on stdin, caches HTML per filter hash
- `app/meta.py`: hostnames / sources / date range for the UI
- `app/main.py`: FastAPI routes
- `app/static/`: vanilla JS UI
- `goaccess.conf`: NPM log format plus the panel settings from the xavierh image

## Performance notes

Filtering is a linear scan over the log files (Python regex, roughly 100k
lines/s). Files whose mtime is before `from`, or whose first line is after
`to`, are skipped without reading. For typical home-lab volumes a report takes
well under a few seconds; the per-filter cache makes repeated views instant.
