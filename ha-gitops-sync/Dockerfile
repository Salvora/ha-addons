# ha-gitops-sync: Home Assistant's base-python image with git and the add-on's package. Built by
# .gitea/workflows/image.yml, which passes BUILD_VERSION, the version in config.yaml.
# Supervisor 2026.04 and later pass no BUILD_FROM, so the base is named here, pinned by digest; Renovate moves it.
FROM ghcr.io/home-assistant/base-python:3.14-alpine3.24@sha256:9022b084ccaa3383cf171cf26a24ce277aeacbdf3e8c5022b6d89e0492a3a897

RUN apk add --no-cache git

COPY requirements.txt /tmp/
RUN pip3 install --no-cache-dir --requirement /tmp/requirements.txt && rm /tmp/requirements.txt

COPY --chmod=755 run.sh /
COPY ha_gitops_sync /app/ha_gitops_sync
# s6-overlay hands a stop to the add-on first instead of killing it 3 s after the rest, so a running deploy finishes
# within config.yaml's timeout.
ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    S6_CMD_RECEIVE_SIGNALS=1

# With the add-on's Watchdog switch on, the Supervisor restarts the add-on when this check fails.
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s \
  CMD ["python3", "-c", "import ssl, urllib.request; c = ssl.create_default_context(); c.check_hostname = False; c.verify_mode = ssl.CERT_NONE; urllib.request.urlopen('https://127.0.0.1:8765/healthz', timeout=5, context=c)"]

ARG BUILD_VERSION
LABEL io.hass.type="app" \
      io.hass.arch="amd64" \
      io.hass.version="${BUILD_VERSION}" \
      io.hass.name="HA GitOps Sync" \
      io.hass.description="Keeps /config and its Gitea repository in step, both ways" \
      io.hass.url="https://github.com/Salvora/ha-addons/tree/main/ha-gitops-sync" \
      org.opencontainers.image.title="ha-gitops-sync" \
      org.opencontainers.image.description="Keeps Home Assistant's /config and its Gitea repository in step, both ways" \
      org.opencontainers.image.licenses="MIT"

CMD ["/run.sh"]
