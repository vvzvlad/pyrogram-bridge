import os

def get_settings():
    tg_api_id = os.getenv("TG_API_ID")
    tg_api_hash = os.getenv("TG_API_HASH")
    if not tg_api_id or not tg_api_hash:
        print("TG_API_ID and TG_API_HASH must be set")
        os._exit(1)

    return {
        "tg_api_id": int(tg_api_id),
        "tg_api_hash": tg_api_hash,
        "session_path": os.getenv("SESSION_PATH", "data") or "data",
        "api_host": os.getenv("API_HOST", "0.0.0.0"),
        "api_port": int(os.getenv("API_PORT") or 8000),
        "pyrogram_bridge_url": os.getenv("PYROGRAM_BRIDGE_URL", ""),
        "log_level": os.getenv("LOG_LEVEL", "INFO"),
        "debug": os.getenv("DEBUG", "False") == "True",
        "token": os.getenv("TOKEN", ""),
        "time_based_merge": os.getenv("TIME_BASED_MERGE", "False").strip() in ["True", "true"],
    } 
