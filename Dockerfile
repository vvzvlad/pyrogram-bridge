FROM python:3.11-slim

WORKDIR /app
RUN mkdir -p data
COPY requirements.txt .
RUN apt-get update && apt-get install -y libmagic-dev git curl --no-install-recommends && apt-get clean && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py .

# Run as a non-root user. Create it, then hand it ownership of /app (including
# data/, so a freshly-initialised named volume mounted at /app/data inherits
# app's uid and the service can still read/write its cache, SQLite and session).
RUN useradd -m -u 1000 app && chown -R app:app /app
USER app

CMD ["python", "api_server.py"]
