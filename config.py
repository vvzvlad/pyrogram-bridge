#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# flake8: noqa
# pylint: disable=broad-exception-raised, raise-missing-from, too-many-arguments, redefined-outer-name
# pylint: disable=multiple-statements, logging-fstring-interpolation, trailing-whitespace, line-too-long
# pylint: disable=broad-exception-caught, missing-function-docstring, missing-class-docstring
# pylint: disable=f-string-without-interpolation, global-statement
# pylance: disable=reportMissingImports, reportMissingModuleSource

import os
import logging
from typing import Any

# Переменная для отслеживания состояния инициализации логгирования
_LOGGING_INITIALIZED = False

def setup_logging(level_name: str = "INFO") -> None:
    global _LOGGING_INITIALIZED 
    
    if _LOGGING_INITIALIZED:  return
        
    level = getattr(logging, level_name.upper(), logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    if not root_logger.handlers:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)
    else:
        for handler in root_logger.handlers:
            handler.setFormatter(formatter)

    logging.getLogger('uvicorn').setLevel(logging.WARNING)
    logging.getLogger('fastapi').setLevel(logging.WARNING)
    
    _LOGGING_INITIALIZED = True
    logging.info("Logging system initialized")

def get_settings() -> dict[str, Any]:
    tg_api_id = os.getenv("TG_API_ID")
    tg_api_hash = os.getenv("TG_API_HASH")
    if not tg_api_id or not tg_api_hash:
        print("TG_API_ID and TG_API_HASH must be set")
        os._exit(1)

    log_level = os.getenv("LOG_LEVEL", "INFO")

    return {
        "tg_api_id": int(tg_api_id),
        "tg_api_hash": tg_api_hash,
        "session_path": os.getenv("SESSION_PATH", "data") or "data",
        "api_host": os.getenv("API_HOST", "0.0.0.0"),
        "api_port": int(os.getenv("API_PORT") or 8000),
        "pyrogram_bridge_url": os.getenv("PYROGRAM_BRIDGE_URL", ""),
        "log_level": log_level,
        "debug": os.getenv("DEBUG", "False") == "True",
        "token": os.getenv("TOKEN", ""),
        "time_based_merge": os.getenv("TIME_BASED_MERGE", "False").strip() in ["True", "true"],
        "show_bridge_link": os.getenv("SHOW_BRIDGE_LINK", "False").strip() in ["True", "true"],
        "show_post_flags": os.getenv("SHOW_POST_FLAGS", "False").strip() in ["True", "true"],
    } 
