#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# flake8: noqa
# pylint: disable=missing-function-docstring

import os
import json
import asyncio
from typing import Any, List


# One shared lock for operations that write to media_file_ids.json
_media_file_ids_lock: asyncio.Lock | None = None


def get_media_file_ids_lock() -> asyncio.Lock:
    global _media_file_ids_lock
    if _media_file_ids_lock is None:
        _media_file_ids_lock = asyncio.Lock()
    return _media_file_ids_lock


def read_json_file_sync(file_path: str) -> List[dict[str, Any]]:
    with open(file_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def write_json_file_sync(file_path: str, data: List[dict[str, Any]]) -> None:
    temp_path = f"{file_path}.tmp"
    with open(temp_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(temp_path, file_path)


