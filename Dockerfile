FROM ghcr.io/xtls/xray-core:26.3.27@sha256:592ec4d11f656db95598d01e76dbcc6e002d67360b96a5436500a938230f52c7 AS xray

FROM python:3.13-slim-bookworm@sha256:67a1e1f215ccda113cfc024e8639049257e88f273898f595b61476d128d387e8

ARG APP_VERSION=0.1.0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    APP_VERSION=${APP_VERSION} \
    XRAY_BINARY=/usr/local/bin/xray \
    OPENCONNECT_BINARY=/usr/sbin/openconnect

LABEL org.opencontainers.image.title="xray2cisco" \
      org.opencontainers.image.description="Xray inbound gateway with Cisco AnyConnect-compatible VPN egress" \
      org.opencontainers.image.version="${APP_VERSION}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates iproute2 openconnect procps tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=xray /usr/local/bin/xray /usr/local/bin/xray
COPY --from=xray /usr/local/share/xray /usr/local/share/xray

WORKDIR /opt/xray2cisco

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY defaults ./defaults
COPY entrypoint.sh ./entrypoint.sh

RUN chmod 0755 /opt/xray2cisco/entrypoint.sh /opt/xray2cisco/scripts/*.sh \
    && mkdir -p /data /run/xray2cisco

EXPOSE 8000 1080 8080
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/opt/xray2cisco/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
