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
import random
import time
from datetime import datetime, timedelta
from typing import Any, Optional, Union, List
from pyrogram import Client
from pyrogram.types import Chat, Message

logger = logging.getLogger(__name__)

# Путь к директории кеша
CACHE_DIR = os.path.join('data', 'tgcache')

def _get_history_cache_file_path(channel_id: Union[str, int], limit: int) -> str:
    """Возвращает путь к файлу кеша истории сообщений для канала с учетом лимита"""
    if not os.path.exists(CACHE_DIR):
        os.makedirs(CACHE_DIR, exist_ok=True)
        logger.info(f"cache_dir_created: path {CACHE_DIR}")
    # Преобразуем в строку для унификации
    channel_id_str = str(channel_id)
    # Заменяем потенциально проблемные символы
    safe_filename = channel_id_str.replace('/', '_').replace('\\', '_')
    return os.path.join(CACHE_DIR, f"{safe_filename}_history_{limit}.cache")

def _save_history_to_cache(channel_id: Union[str, int], messages: List[Message], limit: int) -> None:
    """Сохраняет историю сообщений в кеш"""
    try:
        cache_file = _get_history_cache_file_path(channel_id, limit)
        
        # Создаем метаданные кеша
        cache_data = {
            'timestamp': time.time(),
            'limit': limit,
            'messages': pickle.dumps(messages)
        }
        
        with open(cache_file, 'wb') as f:
            pickle.dump(cache_data, f)
        
        logger.info(f"history_cache_saved: channel {channel_id}, limit {limit}, messages {len(messages)}, file {cache_file}")
    except Exception as e:
        logger.error(f"history_cache_save_error: channel {channel_id}, limit {limit}, error {str(e)}")

def _get_history_from_cache(channel_id: Union[str, int], limit: int, max_age_seconds: int = 300) -> Optional[List[Message]]:
    """
    Получает историю сообщений из кеша если они не старше указанного возраста и соответствуют лимиту
    
    Args:
        channel_id: ID или username канала
        limit: Требуемый лимит сообщений
        max_age_seconds: Максимальный возраст кеша в секундах (по умолчанию 5 минут)
        
    Returns:
        Список сообщений или None если кеш не найден, устарел или лимит не соответствует
    """
    try:
        cache_file = _get_history_cache_file_path(channel_id, limit)
        
        if not os.path.exists(cache_file):
            logger.info(f"history_cache_miss: channel {channel_id}, limit {limit}, cache file not found")
            return None
        
        with open(cache_file, 'rb') as f:
            cache_data = pickle.load(f)
        
        # Проверяем возраст кеша с добавлением случайности
        cache_age = time.time() - cache_data['timestamp']
        # Добавляем случайность до 20% от max_age_seconds
        random_factor = 1 - random.uniform(0, 0.2)
        adjusted_max_age = max_age_seconds * random_factor
        
        if cache_age > adjusted_max_age:
            logger.info(f"history_cache_expired: channel {channel_id}, limit {limit}, age {cache_age:.1f}s > adjusted max {adjusted_max_age:.1f}s (random factor: {random_factor:.2f})")
            return None
        
        # Проверяем соответствие лимита
        cached_limit = cache_data.get('limit')
        if cached_limit != limit:
            logger.info(f"history_cache_limit_mismatch: channel {channel_id}, cached limit {cached_limit}, requested limit {limit}")
            return None
        
        # Восстанавливаем список сообщений из пикла
        messages = pickle.loads(cache_data['messages'])
        logger.info(f"history_cache_hit: channel {channel_id}, limit {limit}, messages {len(messages)}, age {cache_age:.1f}s")
        return messages
    
    except Exception as e:
        logger.error(f"history_cache_read_error: channel {channel_id}, limit {limit}, error {str(e)}")
        # В случае ошибки чтения кеша лучше вернуть None и запросить свежие данные
        return None

async def cached_get_chat_history(client: Client, channel_id: Union[str, int], limit: int = 20, cache_ttl: int = 300) -> List[Message]:
    """
    Получает историю сообщений чата с кешированием.
    
    Args:
        client: Pyrogram клиент
        channel_id: ID или username канала
        limit: Максимальное количество сообщений для получения
        cache_ttl: Время жизни кеша в секундах (по умолчанию 5 минут)
        
    Returns:
        Список сообщений, как и оригинальный client.get_chat_history()
    """
    # Пробуем получить из кеша
    cached_messages = _get_history_from_cache(channel_id, limit, cache_ttl)
    
    if cached_messages is not None:
        return cached_messages
    
    # Если в кеше нет или кеш устарел, запрашиваем через API
    try:
        logger.info(f"history_cache_request: fetching fresh history for channel {channel_id}, limit {limit}")
        messages = []
        async for message in client.get_chat_history(channel_id, limit=limit):
            messages.append(message)
        
        # Сохраняем в кеш
        _save_history_to_cache(channel_id, messages, limit)
        
        return messages
    except Exception as e:
        logger.error(f"history_cache_request_error: channel {channel_id}, limit {limit}, error {str(e)}")
        # Пробрасываем ошибку дальше, как и оригинальный метод
        raise
