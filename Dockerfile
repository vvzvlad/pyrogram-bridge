FROM python:3.11-slim

WORKDIR /app
RUN mkdir -p data
COPY requirements.txt .
RUN apt-get update && apt-get install -y libmagic-dev curl --no-install-recommends && apt-get clean && rm -rf /var/lib/apt/lists/* 
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py .

HEALTHCHECK --interval=10m --timeout=5s --retries=4 --start-interval=2s --start-period=10s CMD curl -f http://localhost:3000/rss/vvzvlad_lytdybr/localhost?limit=1 || exit 1

CMD ["python", "api_server.py"]