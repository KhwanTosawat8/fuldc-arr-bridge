FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/Pete1979/fuldc-arr-bridge"
LABEL org.opencontainers.image.description="Request movies & TV in Seerr/Jellyseerr/Overseerr and auto-download them over Direct Connect via FulDC++"
LABEL org.opencontainers.image.licenses="MIT"

# stdlib-only app — no pip install needed
WORKDIR /app
# webhook flow + CLI
COPY fuldc_client.py ranker.py core.py notify.py plex.py webhook_server.py bridge.py ./
# Radarr/Sonarr flow (arr_server.py, run via the "arr" compose profile)
COPY arr_server.py torznab.py qbit.py store.py ./

USER 1000
EXPOSE 8080 9117
CMD ["python", "webhook_server.py"]
