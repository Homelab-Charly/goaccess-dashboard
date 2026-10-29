# syntax=docker/dockerfile:1
#
# GoAccess filtering webapp for Nginx Proxy Manager logs.
#
# The GeoLite2 database is borrowed from the xavierh/goaccess-for-nginxproxymanager
# image so the geo panels work without a MaxMind account. Remove the
# `COPY --from=geoip` line below to build without it (or mount your own
# database at /geoip/GeoLite2-City.mmdb at runtime).

ARG GEOIP_SOURCE=docker.io/xavierh/goaccess-for-nginxproxymanager:latest
FROM ${GEOIP_SOURCE} AS geoip

FROM python:3.12-alpine

RUN apk add --no-cache goaccess tzdata libmaxminddb curl \
    && goaccess --version

WORKDIR /srv
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# GeoLite2 (optional; the app auto-detects /geoip/*.mmdb)
RUN mkdir -p /geoip
COPY --from=geoip /goaccess-config/GeoLite2-City.mmdb /geoip/GeoLite2-City.mmdb

COPY goaccess.conf ./goaccess.conf
COPY app ./app

ENV LOG_DIR=/opt/log \
    CACHE_DIR=/cache \
    CACHE_TTL=60 \
    SKIP_ARCHIVED_LOGS=False \
    TZ=UTC \
    PYTHONUNBUFFERED=1

VOLUME ["/opt/log", "/cache"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fs http://localhost:8000/healthz || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
