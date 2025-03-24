FROM python:3.11-slim

WORKDIR /app
RUN mkdir -p data
COPY requirements.txt .
RUN apt-get update && apt-get install -y libmagic-dev curl --no-install-recommends && apt-get clean && rm -rf /var/lib/apt/lists/* 
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py .

CMD ["python", "api_server.py"]