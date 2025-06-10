#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# flake8: noqa
# pylint: disable=broad-exception-raised, raise-missing-from, too-many-arguments, redefined-outer-name
# pylint: disable=multiple-statements, logging-fstring-interpolation, trailing-whitespace, line-too-long
# pylint: disable=broad-exception-caught, missing-function-docstring, missing-class-docstring
# pylint: disable=f-string-without-interpolation
# pylance: disable=reportMissingImports, reportMissingModuleSource


import os
import json
import pickle
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Optional, Union
from pyrogram import Client
from pyrogram.types import Chat, Message

logger = logging.getLogger(__name__)

# Путь к директории кеша
CACHE_DIR = os.path.join('data', 'tgcache')

def _get_cache_file_path(channel_id: Union[str, int]) -> str:
    """Возвращает путь к файлу кеша для канала"""
    if not os.path.exists(CACHE_DIR):
        os.makedirs(CACHE_DIR, exist_ok=True)
        logger.info(f"cache_dir_created: path {CACHE_DIR}")
    # Преобразуем в строку для унификации
    channel_id_str = str(channel_id)
    # Заменяем потенциально проблемные символы
    safe_filename = channel_id_str.replace('/', '_').replace('\\', '_')
    return os.path.join(CACHE_DIR, f"{safe_filename}.cache")

def _save_to_cache(channel_id: Union[str, int], chat_data: Chat) -> None:
    """Сохраняет данные чата в кеш"""
    try:
        cache_file = _get_cache_file_path(channel_id)
        
        # Создаем метаданные кеша
        cache_data = {
            'timestamp': time.time(),
            'chat_data': pickle.dumps(chat_data)
        }
        
        with open(cache_file, 'wb') as f:
            pickle.dump(cache_data, f)
        
        logger.info(f"chat_cache_saved: channel {channel_id}, file {cache_file}")
    except Exception as e:
        logger.error(f"cache_save_error: channel {channel_id}, error {str(e)}")

def _get_from_cache(channel_id: Union[str, int], max_age_seconds: int = 86400) -> Optional[Chat]:
    """
    Получает данные чата из кеша если они не старше указанного возраста
    
    Args:
        channel_id: ID или username канала
        max_age_seconds: Максимальный возраст кеша в секундах (по умолчанию 1 день)
        
    Returns:
        Chat объект или None если кеш не найден или устарел
    """
    try:
        cache_file = _get_cache_file_path(channel_id)
        
        if not os.path.exists(cache_file):
            logger.info(f"chat_cache_miss: channel {channel_id}, cache file not found")
            return None
        
        with open(cache_file, 'rb') as f:
            cache_data = pickle.load(f)
        
        # Проверяем возраст кеша
        cache_age = time.time() - cache_data['timestamp']
        if cache_age > max_age_seconds:
            logger.info(f"chat_cache_expired: channel {channel_id}, age {cache_age:.1f}s > max {max_age_seconds}s")
            return None
        
        # Восстанавливаем объект Chat из пикла
        chat_data = pickle.loads(cache_data['chat_data'])
        logger.info(f"chat_cache_hit: channel {channel_id}, age {cache_age:.1f}s")
        return chat_data
    
    except Exception as e:
        logger.error(f"cache_read_error: channel {channel_id}, error {str(e)}")
        # В случае ошибки чтения кеша лучше вернуть None и запросить свежие данные
        return None

def adapt_chat_for_get_username(chat: Chat) -> Chat:
    """
    Адаптирует объект Chat для использования в методе get_channel_username
    
    Метод get_channel_username ожидает объект Message, но использует только его атрибут chat,
    поэтому мы можем передать сам объект Chat без обертки
    """
    return chat

async def cached_get_chat(client: Client, channel_id: Union[str, int], cache_ttl: int = 86400) -> Chat:
    """
    Получает информацию о чате с кешированием.
    
    Args:
        client: Pyrogram клиент
        channel_id: ID или username канала
        cache_ttl: Время жизни кеша в секундах (по умолчанию 1 день)
        
    Returns:
        Chat объект, как и оригинальный client.get_chat()
    """
    # Пробуем получить из кеша
    cached_chat = _get_from_cache(channel_id, cache_ttl)
    
    if cached_chat is not None:
        return cached_chat
    
    # Если в кеше нет или кеш устарел, запрашиваем через API
    try:
        logger.info(f"chat_cache_request: fetching fresh data for channel {channel_id}")
        chat_data = await client.get_chat(channel_id)
        
        # Сохраняем в кеш
        _save_to_cache(channel_id, chat_data)
        
        return chat_data
    except Exception as e:
        logger.error(f"chat_request_error: channel {channel_id}, error {str(e)}")
        # Пробрасываем ошибку дальше, как и оригинальный метод
        raise
