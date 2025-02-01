FROM python:3.11-slim

WORKDIR /app
RUN mkdir -p data
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py .

CMD ["uvicorn", "api_server:app", "--host", "0.0.0.0", "--port", "8000"] 