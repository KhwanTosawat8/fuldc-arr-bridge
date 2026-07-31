FROM python:3.12-slim

# stdlib-only app — no pip install needed
WORKDIR /app
COPY fuldc_client.py ranker.py core.py notify.py plex.py webhook_server.py bridge.py ./

USER 1000
EXPOSE 8080
CMD ["python", "webhook_server.py"]
