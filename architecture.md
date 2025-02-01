# Telegram RSS Bridge Architecture

## System Components

### 1. Web Parser (Main Bridge)
- Existing web-based parser for public Telegram content
- Fallback mechanism to Pyro Parser when content not found

### 2. Pyro Parser (Backup Service)
- Pyrogram-based client for private content
- HTTP API server for post retrieval
- Session management for Telegram auth

### 3. HTTP API Interface
- Endpoints:
  - `GET /post/{channel}/{post_id}` - Get single post
  - `GET /channel/{channel}` - (Future) Get channel feed
- Response format:
  ```json
  {
    "id": 123,
    "date": "2023-01-01T00:00:00",
    "text": "post content",
    "media": [],
    "views": 100,
    "reactions": []  // Future extension
  }
  ```

## Service Structure
```text
pyro_parser/
├── telegram_client.py   # Pyrogram wrapper + session management
├── api_server.py        # FastAPI/Falcon server
├── schemas.py           # Data models/Pydantic schemas
└── config.py            # Environment configuration
```

## Key Features

1. **Pyrogram Client**
- Async Telegram client with session persistence
- Automatic reconnection
- Rate limiting handling
- Error tracking for API calls

2. **Content Processing**
- Raw post transformation
- Media links extraction
- Text cleanup utilities
- (Future) Reactions parser

## Environment Configuration
```bash
# Core settings
TG_API_ID=123456
TG_API_HASH=abcdef123456
SESSION_PATH=/data/session.file

# Server config
API_HOST=0.0.0.0
API_PORT=8000
REQUEST_TIMEOUT=30
```

## Docker Setup
```yaml
services:
  pyro_parser:
    image: ghcr.io/vvzvlad/pyrotg-bridge:latest
    environment:
      TG_API_ID: ${TG_API_ID}
      TG_API_HASH: ${TG_API_HASH}
    volumes:
      - ./sessions:/data
    ports:
      - "8000:8000"
```

## CI/CD Integration
```yaml
# .github/workflows/deploy.yml
name: Deploy
on: [push]

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - name: Build and push
        uses: docker/build-push-action@v4
        with:
          context: .
          push: true
          tags: ghcr.io/vvzvlad/pyrotg-bridge:latest
```

## Extension Points

1. **Post Processing**
- Media download hooks
- Link preview generators
- Content filters

2. **API Features**
- Pagination params
- Field selection
- Webhook support

3. **Monitoring**
- /status endpoint
- Basic request metrics
- Error logging
