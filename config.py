import os

TG_API_ID = int(os.getenv("TG_API_ID"))
TG_API_HASH = os.getenv("TG_API_HASH")
SESSION_PATH = os.getenv("SESSION_PATH", "session.file")
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", 8000))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", 30))
SESSION_STRING = os.getenv("TG_SESSION_STRING", "")

DEFAULT_CONFIG = {
    "session_string": "",
    "tg_api_id": 12345,
    "tg_api_hash": "xxxxxxxx",
    "file_access_flags": ["can_access_files"]
}

def get_settings():
    return {
        "tg_api_id": TG_API_ID,
        "tg_api_hash": TG_API_HASH,
        "session_path": SESSION_PATH,
        "api_host": API_HOST,
        "api_port": API_PORT,
        "request_timeout": REQUEST_TIMEOUT,
        "session_string": SESSION_STRING
    } 