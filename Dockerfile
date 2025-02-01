FROM python:3.11-slim

WORKDIR /app
RUN mkdir -p data
RUN apt-get update && apt-get install -y curl libgl1 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py .

CMD ["uvicorn", "api_server:app", "--host", "0.0.0.0", "--port", "8000"] 