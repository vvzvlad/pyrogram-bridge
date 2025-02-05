import os

def get_settings():
    return {
        "tg_api_id": int(os.getenv("TG_API_ID")),
        "tg_api_hash": os.getenv("TG_API_HASH"),
        "session_path": os.getenv("SESSION_PATH", "session.file") or "session.file",
        "api_host": os.getenv("API_HOST", "0.0.0.0"),
        "api_port": int(os.getenv("API_PORT") or 8000),
        "session_string": os.getenv("TG_SESSION_STRING", ""),
        "pyrogram_bridge_url": os.getenv("PYROGRAM_BRIDGE_URL", ""),
        "log_level": os.getenv("LOG_LEVEL", "INFO"),
        "debug": os.getenv("DEBUG", "False") == "True",
        "token": os.getenv("TOKEN", "")
    } 
