
### Где и почему может виснуть

- Синхронный диск/JSON в обработчиках запросов
  - `_save_media_file_ids` делает `open/json.load/json.dump` прямо в пути запроса, без `to_thread`. При большом `data/media_file_ids.json` это блокирует event loop.
```950:979:post_parser.py
                        if os.path.exists(file_path):
                            with open(file_path, 'r', encoding='utf-8') as f:
                                existing_data = json.load(f)
...
                        with open(file_path, 'w', encoding='utf-8') as f:
                            json.dump(existing_data, f, ensure_ascii=False, indent=2)
```
  - Кеш истории также читает/пишет файлы синхронно:
```71:99:tg_cache.py
        with open(cache_file, 'rb') as f:
            cache_data = pickle.load(f)
...
        with open(cache_file, 'wb') as f:
            pickle.dump(cache_data, f)
```
  - Итог: под нагрузкой любой долгий sync I/O останавливает обработку всех запросов на время операции.

- CPU‑тяжёлое формирование RSS/HTML в event loop
  - Парсинг/санитизация/генерация RSS идут синхронно в корутинах (bleach, feedgen, склейка больших строк). На больших лимитах это “съедает” цикл.
```185:299:rss_generator.py
final_posts = await _render_messages_groups(...)
...
rss_feed = fg.rss_str(pretty=True)
```
```511:533:rss_generator.py
final_posts = await _render_messages_groups(...)
...
html = '\n<hr class="post-divider">\n'.join(html_posts)
```

- Глобальный `python-magic` в потоках
  - Один общий `Magic()` используется из разных потоков — это не потокобезопасно, возможны зависания внутри libmagic.
```198:206:api_server.py
media_type = await asyncio.to_thread(magic_mime.from_file, file_path)
```

- BaseHTTPMiddleware над стримингом файлов
  - `BaseHTTPMiddleware` оборачивает `call_next`. Для `FileResponse` это может ломать/буферизовать стриминг и усиливать задержки.
```48:62:api_server.py
class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        ...
        response = await call_next(request)
```

- Отсутствие таймаутов на вызовах Telegram API
  - Если `get_messages/download_media` “подвисли”, запрос к `/media` висит долго.
```748:758:api_server.py
file_path, delete_after = await download_media_file(...)
```
```232:258:api_server.py
message = await client.client.get_messages(...)
file_path = await client.client.download_media(...)
```

- Шумное логирование в middleware
  - Лог всех заголовков и запросов при каждом хите может создавать I/O‑бутылочное горлышко под нагрузкой.
```49:59:api_server.py
logger.info(f"Request: {request.method} {request.url}")
logger.info(f"Headers: {dict(request.headers)}")
```

- Конкурентная запись и разрастание `media_file_ids.json`
  - Записи без блокировок → гонки и порча JSON. В фоне потом ремонт json-repair (тяжёлый CPU).
```503:511:api_server.py
except json.JSONDecodeError:
    media_files = await asyncio.to_thread(fix_corrupted_json, file_path)
```

- Один воркер uvicorn
  - Любая тяжёлая задача “монополизирует” процесс — остальное, включая отдачу файлов, ждёт.
```114:121:api_server.py
uvicorn.run(..., reload=True, loop="uvloop")
```

### Что сделать (короткий чеклист)

- [x] Убрать sync I/O из пути запроса
  - [x] Обернуть чтение/запись JSON и pickle в `asyncio.to_thread`.
  - [x] Для `media_file_ids.json` везде использовать единые sync‑хелперы с временным файлом и заменить прямые `open/json.dump`:
    - [x] Вынести в отдельный модуль функции уже имеющихся `read_json_file_sync`/`write_json_file_sync` и звать их через `to_thread`.
  - [x] Добавить один `asyncio.Lock` на операции записи `media_file_ids.json` чтобы не было гонок.

- Разгрузить CPU из event loop
  - В RSS/HTML:
    - После того как данные получены из Telegram, вынести “склейку HTML”, bleach‑санитизацию и `fg.rss_str` в `asyncio.to_thread`.
    - Ограничить `limit` по умолчанию до 50–80. Для больших значений — отдавать 429 или 400 с рекомендацией.
    - Опционально — кэшировать итоговые RSS/HTML на пару минут.

- Исправить `python-magic`
  - Создавать `Magic()` per‑call внутри `to_thread` ИЛИ защищать глобальный объект `threading.Lock`.
  - Если не критично — падать на `mimetypes` и не вызывать `magic` для известных расширений.

- Middleware для логов
  - В проде отключить текущий `RequestLoggingMiddleware` или перевести на ASGI‑middleware без вмешательства в стриминг.
  - Снизить уровень и объём логов (заголовки — только на DEBUG, семплирование).

- Таймауты телеграма
  - Оборачивать `get_messages`/`download_media` в `asyncio.wait_for(..., timeout=30–60)` с понятной ошибкой/503.
  - На FloodWait в `/media` — отдавать 429 с `Retry-After`.

- Запись `media_file_ids.json`
  - Причесать размер: периодически чистить/ротация по размеру/возрасту.
  - В фоне перепись файла уже есть — не трогать event loop; убедиться, что на записи тоже используется temp‑файл + `os.replace` (как в `write_json_file_sync`).

- Процессный параллелизм
  - Запускать с несколькими воркерами, без `reload`:
    - пример: `uvicorn api_server:app --host 0.0.0.0 --port 80 --workers 2 --loop uvloop --http httptools`
  - Или за Gunicorn: `--workers 2 --worker-class uvicorn.workers.UvicornWorker`.

- Пул потоков
  - Явно ограничить/увеличить пул для `to_thread` если есть много фоновых задач, чтобы не выжирался из‑за долгих операций.

### Минимальные целевые правки (по месту)

- `_save_media_file_ids` — переписать на `await asyncio.to_thread(read_json_file_sync/ write_json_file_sync)` + общий `asyncio.Lock`. Убрать прямые `open/json.dump`.
- `tg_cache` — обернуть `pickle.load/dump` в `to_thread`.
- `prepare_file_response` — перестать шарить `magic_mime`; создавать локальный `magic.Magic(mime=True)` внутри `to_thread` или защитить глобальный объект `Lock`.
- `RequestLoggingMiddleware` — удалить/заменить на ASGI‑middleware и логировать только метод+путь, без заголовков по умолчанию.
- `get_messages/download_media` — таймауты через `asyncio.wait_for`.
- `uvicorn.run` — убрать `reload=True`, добавить `workers=2+`.

### Как быстро проверить
- Нагрузить `/rss` c большим `limit` параллельно с `/media` и измерить задержку отдачи файлов до/после:
  - до фиксов статик “замирает” во время генерации;
  - после выноса CPU/I/O в `to_thread` и увеличения воркеров — файлы продолжают отдаваться.

Коротко: основные причины — синхронный диск и CPU в корутинах, общий `python-magic`, и единичный воркер. Перенос I/O/CPU в `to_thread`, фиксы гонок JSON, таймауты на Telegram и 2–4 воркера решат подвисания.

- Нашёл проблемные места: sync I/O и CPU в обработчиках, `python-magic`, middleware, таймауты и один воркер. Дал точечные действия для устранения подвисаний.