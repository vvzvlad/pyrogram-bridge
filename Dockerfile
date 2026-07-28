FROM python:3.11-slim

WORKDIR /app
RUN mkdir -p data
COPY requirements.txt .
# util-linux is pinned explicitly for `setpriv` (used by entrypoint.sh to drop to uid 1000);
# the slim base ships it today, but depending on that implicitly would silently break the
# container start (`setpriv: not found`) if a future base bump drops it.
RUN apt-get update && apt-get install -y libmagic-dev git curl util-linux --no-install-recommends && apt-get clean && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py .
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Create the non-root runtime user and hand it ownership of the /app image layer
# (including data/, so a freshly-initialised named volume mounted at /app/data
# inherits app's uid and the service can read/write its cache, SQLite and session).
RUN useradd -m -u 1000 app && chown -R app:app /app

# NOTE: we intentionally do NOT `USER app` here. The chown above only touches the
# image layer, not a pre-existing named volume mounted over /app/data. Older
# installs (pre-#64 root image) have root-owned files on that volume; if we
# started as `app` the service could not write them and SQLite would crash-loop
# with "readonly database" (issue #82). So the container starts as ROOT, and
# entrypoint.sh chowns /app/data before dropping to uid 1000 to exec the app.
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "api_server.py"]
