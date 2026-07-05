# План стабилизации pyrogram-bridge: статика + зависания

Дата: 2026-07-05. База: аудит api_server.py / telegram_client.py / tg_throttle.py / tg_cache.py / file_io.py / rss_generator.py / post_parser.py.

Проверенные факты, на которых строится план:
- `FloodWait` — подкласс `RPCError` (проверено на установленном Kurigram).
- Starlette 0.45.3 поддерживает Range в `FileResponse` из коробки (проверено по исходникам в venv).
- Дефолтный executor `asyncio.to_thread` = min(32, cpu+4) потоков; в контейнере на 1–2 CPU это 5–6.
- В venv стоит Kurigram 2.2.4, в requirements закреплён 2.2.22 — локальное окружение не соответствует прод-образу.

## Целевые инварианты (что должно стать верным после ремонта)

1. Любой файл в кэше с финальным именем (`{file_unique_id}` или `temp_{file_unique_id}`) — гарантированно полный. Частичным может быть только `*.part.*`.
2. Каждый Telegram RPC ограничен таймаутом. Глобальный RPC-гейт не может удерживаться дольше таймаута.
3. Временные ошибки Telegram (FloodWait) никогда не превращаются в постоянные HTTP-ответы (404).
4. Event loop не выполняет CPU-работу > ~50 мс за раз.
5. Каждая фоновая задача supervised: её смерть видна в логах на CRITICAL и/или она перезапускается.
6. Healthcheck не зависит от Telegram RPC и от обхода файловой системы.

## Процесс

- Ветка `fix/stability` от `main`, коммит на каждую стадию (на `main` не коммитим).
- Порядок стадий = порядок деплоя: после стадий 1 и 2 уже можно выкатываться и наблюдать.
- После каждой стадии: pytest + ручные сценарии из стадии 7 + код-ревью.
- Перед началом: пересоздать/обновить venv под requirements (Kurigram 2.2.22), прогнать 174 существующих теста как baseline. Добавить dev-зависимости: pytest-asyncio, httpx (для TestClient).

---

## Стадия 1 — устранение зависаний (минимальный диф, максимальный эффект)

### 1.1 Таймауты на все RPC под глобальным гейтом

Файлы: tg_cache.py, config.py.

- Добавить в config `tg_rpc_timeout` (env `TG_RPC_TIMEOUT`, default 60).
- `cached_get_chat_history` (tg_cache.py:142-144): итерацию истории собрать в корутину и обернуть в `asyncio.wait_for` **внутри** `async with tg_rpc()`:
  ```python
  async with tg_rpc():
      async def _collect():
          # Full paginated history; bounded by the outer wait_for
          return [m async for m in client.get_chat_history(channel_id, limit=limit)]
      messages = await asyncio.wait_for(_collect(), timeout=Config["tg_rpc_timeout"])
  ```
- `cached_get_chat` (tg_cache.py:216-217): `await asyncio.wait_for(client.get_chat(channel_id), timeout=...)` внутри гейта.

**Где не наебаться:**
- `wait_for` должен оборачивать **только RPC**, не `_sem.acquire()`. Ожидание в очереди гейта — легитимный backpressure; если холдер ограничен таймаутом, очередь всегда дренируется. Обернёшь acquire — получишь ложные таймауты при штатной очереди из 47 фидов miniflux.
- `async for` нельзя завернуть в wait_for напрямую — только через промежуточную корутину (как в эскизе).
- CancelledError не глотать: `except Exception` в вызывающих местах её и так не ловит (это BaseException в 3.11) — не «улучшать» до `except BaseException`.

### 1.2 Таймауты и гейт на остальные живые RPC

- `_reply_enrichment` (rss_generator.py:505): обернуть `client.get_messages` в `async with tg_rpc()` + `wait_for`.
- `PostParser.get_post` (post_parser.py:98): `wait_for(..., 30)`; заодно удалить `print(...)` (post_parser.py:95).
- `/health` (api_server.py:906): `wait_for(client.client.get_me(), 10)`.

### 1.3 Починка фонового воркера и очереди

Файл: api_server.py.

- `background_download_worker` (620-637): `task_done()` вызывать только если элемент был реально получен:
  ```python
  while True:
      item = await download_queue.get()   # cancellation propagates cleanly here
      channel, post_id, file_unique_id = item
      try:
          async with BACKGROUND_DOWNLOAD_SEMAPHORE:
              await download_media_file(channel, post_id, file_unique_id)
          await asyncio.sleep(2)
      except errors.FloodWait as e:
          logger.warning(f"bg_download_floodwait: sleeping {e.value}s")
          await asyncio.sleep(min(int(e.value) + 5, 900))
      except Exception as e:
          logger.error(f"Background download error ...: {e}")
      finally:
          download_queue.task_done()
  ```
- `download_new_files` (605-609): `await download_queue.put(...)` → `download_queue.put_nowait(...)` — иначе `except asyncio.QueueFull` остаётся мёртвым кодом, а заполненная очередь навсегда блокирует весь `cache_media_files` (включая удаление старых файлов).

**Где не наебаться:**
- Сейчас смерть воркера убивает и уборку кэша (цепочка: воркер умер → очередь заполнилась → `await put()` завис → свипер больше не крутится). После фикса проверить тестом: воркер, у которого download постоянно бросает исключение, продолжает жить и `queue.join()` завершается.
- FloodWait в воркере надо ловить **до** Exception и спать, иначе воркер будет молотить телеграм под флудом.

### 1.4 Supervision фоновых задач

- В lifespan повесить `add_done_callback` на `background_task` и `worker_task`: если таск завершился не через CancelledError — лог CRITICAL с exception + перезапуск таска (обёртка `_supervised(factory)` с ограничением частоты рестартов, например не чаще раза в 60 с).

### Тесты стадии 1
- Гейт: мок «зависшего» RPC (asyncio.Event, который никогда не сеттится) → первый вызов отваливается по таймауту, второй проходит; пермит не утёк.
- Воркер: download бросает Exception/FloodWait → воркер жив, task_done сбалансирован.
- `_TgRpcGate`: отмена во время ожидания spacing не теряет пермит (уже реализовано — закрепить тестом).

### DoD стадии 1
Ни один путь кода не ждёт Telegram без таймаута; воркер не умирает молча; pytest зелёный.

---

## Стадия 2 — статика: большие видео, FloodWait, семафор

### 2.1 Единый путь скачивания с атомарным rename (главный фикс флаки)

Файл: api_server.py, telegram_client.py.

Сейчас большие видео (>100MB) качаются напрямую в финальное имя `temp_{fid}` (379-396): конкурентный запрос видит частичный файл и отдаёт его; при таймауте обрезок не удаляется и целый час отдаётся как «готовый».

- Выделить хелпер:
  ```python
  async def _download_atomic(file_id: str, final_path: str, timeout: float) -> str:
      # Download to a unique partial path, validate, atomically rename.
      part_path = f"{final_path}.part.{uuid.uuid4().hex}"
      try:
          await client.safe_download_media(file_id, part_path, timeout=timeout)
          if not os.path.exists(part_path) or os.path.getsize(part_path) == 0:
              raise ZeroSizeFileError(...)
          if not os.path.exists(final_path):
              os.rename(part_path, final_path)  # atomic on POSIX
          return final_path
      finally:
          # Always clean up our partial file (timeout, cancel, race loser)
          if os.path.exists(part_path):
              try: os.remove(part_path)
              except OSError: pass
      ```
- Использовать его и для обычных файлов, и для больших видео (финальное имя больших остаётся `temp_{fid}` — семантика «не кэшировать постоянно, чистить по TTL» сохраняется).
- `safe_download_media` научить принимать `timeout` параметром; для больших видео таймаут от размера: `min(1800, max(120, file_size // (256*1024)))` (≈256 KB/s минимально допустимая скорость), env-конфигурируемо.
- Проверку `if os.path.exists(temp_file_path): return` (382-384) заменить: существующий `temp_{fid}` теперь **гарантированно полный** (появляется только через rename) — отдавать можно смело.

**Где не наебаться:**
- Единый суффикс `.part.{hex}` вместо старого `.tmp.{hex}`. Обновить regex свипера (api_server.py:536) на новый суффикс, **оставив** и старый паттерн на переходный период (на диске могут лежать старые обрезки).
- Инвариант «финальное имя = полный файл» ломается, если хоть один путь пишет мимо `.part.` — после правки grep-ом убедиться, что `download_media` больше нигде не получает финальный путь.
- В `download_new_files` (598-600) проверка «temp существует → скипаем» остаётся корректной: полный файл есть — качать не надо. Частичные `.part.` под неё не попадают — это правильно.
- Первый запрос большого видео всё ещё отвечает только после полного скачивания (минуты). Это осознанное ограничение; прогрессивный стриминг через `client.stream_media` — отдельная большая фича, в этот план не входит (пометить как future work).

### 2.2 Дедупликация конкурентных скачиваний (in-flight registry)

- Модульный `_inflight: dict[tuple[str, int, str], asyncio.Future]`.
- Первый запрос: создаёт future, запускает скачивание **отдельным** `asyncio.create_task` (не в контексте HTTP-запроса!), в finally сеттит result/exception и удаляет ключ. Остальные: `await`ят future.

**Где не наебаться (классическая ловушка):**
- Если качать прямо в корутине первого HTTP-запроса, отключение его клиента отменит скачивание, future никогда не завершится, и все ожидающие повиснут. Поэтому: скачивание — в detached task; ожидающие делают `await asyncio.wait_for(fut, timeout)`.
- В finally у task обязательно и `set_exception`, и `pop` ключа — иначе после первой ошибки ключ навсегда «занят» отработавшим future.
- `set_exception` на future, который никто не await-ит, даст "exception was never retrieved" warning — допустимо, но можно гасить через `fut.exception()` в done-callback.

### 2.3 FloodWait → 429 в /media

- В `get_media` добавить обработчик **до** `except errors.RPCError` (api_server.py:1001):
  ```python
  except errors.FloodWait as e:
      retry_after = min(int(e.value) + random.randint(1, 30), 300)
      return Response(status_code=429, content="Telegram flood wait",
                      headers={"Retry-After": str(retry_after)})
  ```

**Где не наебаться:** порядок except-веток решает: FloodWait — подкласс RPCError, поставишь после — не сработает никогда. Закрепить тестом (мок download_media_file бросает FloodWait → ответ 429, не 404).

### 2.4 Не удалять большое видео «из-под зрителя»

- При каждой отдаче файла с именем `temp_*` — `await asyncio.to_thread(os.utime, path)` (touch mtime). Свипер (порог 1 час по mtime) перестанет удалять активно просматриваемые файлы.

### 2.5 Ограничить ожидание HTTP-семафора

- Вместо `async with HTTP_DOWNLOAD_SEMAPHORE`: явный `acquire` под `wait_for(30)`; таймаут → 503 + `Retry-After: 30`. Release — только если acquire удался (try/finally вокруг критической секции, а не вокруг acquire).

### Тесты стадии 2
- Конкурентные запросы большого видео с медленным фейковым download: второй запрос НЕ получает частичный файл (либо ждёт future, либо 503/504), после завершения оба получают полный.
- Таймаут download → `.part.` удалён, финального имени нет.
- FloodWait → 429 c Retry-After.
- Свипер: чистит и `.part.`, и старые `.tmp.`, не трогает свежие.

### DoD стадии 2
Ни при каком сценарии клиенту не может быть отдан неполный файл; флуд отдаёт 429; обрезки не живут дольше часа.

---

## Стадия 3 — отдача файлов через FileResponse, чистка HTTP-слоя

### 3.1 Заменить самодельный стриминг на `FileResponse`

Файл: api_server.py (`prepare_file_response`, 203-335).

Starlette 0.45.3 сам обрабатывает Range/If-Range/206/416/multipart, ставит Accept-Ranges/ETag/Last-Modified и читает файл эффективно (без нашего to_thread на каждые 64KB):

```python
return FileResponse(
    file_path,
    media_type=media_type,
    filename=os.path.basename(file_path),        # sets Content-Disposition: inline; filename*
    content_disposition_type="inline",
    headers={"Cache-Control": "public, max-age=86400, immutable"},
    background=background,
)
```

Оставить: пре-чек существования (404), MIME-логику (magic + SQLite-кэш типа). Удалить: ручной парсинг Range, `file_chunk_generator`, ручные заголовки.

**Где не наебаться:**
- **Сначала тесты, потом свап.** Написать httpx TestClient-тесты на текущее поведение (bytes=0-499, bytes=500-, bytes=-500, start за EOF → 416, мусорный заголовок), затем перейти на FileResponse и осознанно принять расхождения (например, Starlette на кривой заголовок может отдать 200-полный вместо 416 — это допустимо по RFC 7233).
- FileResponse проверяет файл на этапе отправки: пре-чек на 404 оставить обязательно.
- `filename*=UTF-8''...` FileResponse формирует сам — руками Content-Disposition не собирать, иначе задвоится.

### 3.2 Убрать BaseHTTPMiddleware

- `RequestLoggingMiddleware` (api_server.py:54-64) удалить. Логирование запросов: либо `--access-log` uvicorn на debug-уровне, либо чистый ASGI-middleware из ~10 строк (без BaseHTTPMiddleware): меньше оверхеда на каждый чанк стриминга и никаких сюрпризов с отменой/фоновыми тасками.

### 3.3 Расширить дефолтный executor

- В lifespan: `loop.set_default_executor(ThreadPoolExecutor(max_workers=32, thread_name_prefix="io"))`. Даже после ухода per-chunk чтений остаются SQLite/magic/pickle/os.walk — 5–6 дефолтных потоков в контейнере мало.

### DoD стадии 3
Range-тесты зелёные; profiling-запрос большого файла не порождает поток-на-чанк; поведение эндпоинта эквивалентно (кроме осознанных RFC-допущений).

---

## Стадия 4 — гигиена event loop (генерация фидов)

### 4.1 `raw_message` — лениво

- `process_message(..., include_raw: bool = False)`: `str(message)` (полная сериализация каждого поста! post_parser.py:523) выполнять только для JSON-выдачи и debug-HTML. В фидах — никогда.

### 4.2 Убрать side-effect IO из process_message (обезвредить ловушку до 4.3)

Сейчас `_generate_html_media` → `_save_media_file_ids` (post_parser.py:676, 977-1028) делает `asyncio.get_running_loop()` + `create_task`. **Если перенести рендер в поток (4.3) без этой правки, `get_running_loop()` бросит RuntimeError, ветка упадёт в `except` с логом — и записи media id молча перестанут сохраняться, а фоновый прогрев кэша умрёт.**

- Рефакторинг: `_save_media_file_ids` не пишет в БД, а складывает `(channel, post_id, file_unique_id, ts)` в `self._pending_media_ids` инстанса PostParser (инстанс создаётся на запрос — потокобезопасно, т.к. рендер идёт в одном потоке).
- После рендера вызывающий код (rss_generator / get_post) один раз делает `await asyncio.to_thread(upsert_media_file_ids_bulk_sync, DB_PATH, entries)` — новая функция в file_io.py с `executemany`. Это заодно даёт батчинг апсертов (часть стадии 5).
- Счётчик `_persist_pending_count` и create_task-механику удалить.

### 4.3 Рендер фида — в поток

- `_create_time_based_media_groups`, `_create_messages_groups`, `_trim_messages_groups`, `_render_messages_groups` — внутри нет ни одного await; превратить в обычные sync-функции и выполнять одним `await asyncio.to_thread(_render_pipeline, ...)` из `generate_channel_rss` / `generate_channel_html`.
- `copy.deepcopy(messages)` (rss_generator.py:38) уезжает в поток вместе со всем остальным.

**Где не наебаться:**
- GIL: перенос CPU-работы в поток не делает её бесплатной — луп перестаёт стоять колом, но замедляется. Поэтому 4.1/4.4 (сокращение работы) обязательны, а не опциональны.
- Внутри потока не должно остаться ни `create_task`, ни `get_running_loop` (это ровно 4.2), ни обращений к asyncio-примитивам.
- deepcopy живых Pyrogram-объектов сегодня работает — код не менять, только переместить; отдельным тестом убедиться, что deepcopy Message из pickle-кэша не падает. Полный отказ от deepcopy (не-мутирующая группировка через map message_id → group_id) — отдельный опциональный рефакторинг, в эту стадию не тащить.

### 4.4 Сократить sanitize-проходы

Сейчас на каждое сообщение bleach вызывается 4+ раз (body: post_parser.py:672, media: 752, footer: 833, reactions: 920), плюс финальный проход по всему фиду. Bleach — самая дорогая CPU-часть (есть diag_sanitize_slow > 50 мс на вызов).

- Правило: **один sanitize на каждую выходную границу**, не на каждый фрагмент:
  - RSS/HTML-фиды: финальный проход уже есть (rss_generator.py:443, 643) → внутренние per-message проходы убрать.
  - `/html/{channel}/{post_id}`: единственный проход добавить в `_format_html` (или в эндпоинте) — сейчас он полагается на внутренние.
  - `/json/...`: html-поля внутри JSON должны оставаться чищеными → один проход по body+footer в `process_message`.

**Где не наебаться (XSS!):**
- Перед удалением внутренних проходов построить карту «выходная точка → где чистится», и только потом резать. Ни один выходной путь не должен остаться без ровно одного прохода.
- Добавить тесты безопасности: мок-сообщение с `<script>`, `onerror=`, `javascript:`-ссылкой → во всех трёх выводах (rss, html, json) — вычищено.
- Помнить, что `debug=true` HTML вставляет `raw_message` в `<pre>` без экранирования — при 4.1 заодно прогнать через `html.escape`.

### DoD стадии 4
Генерация фида на 100 сообщений не блокирует луп заметно (проверка: параллельный запрос `/ping` из стадии 6 отвечает < 100 мс во время генерации); XSS-тесты зелёные; media id продолжают сохраняться (тест на bulk upsert после рендера).

---

## Стадия 5 — SQLite: батчинг вместо записи на каждый чих

### 5.1 Access-time аккумулятор

- Сейчас каждый кэш-хит /media = поток + connect + UPDATE (api_server.py:963-973). Заменить на модульный `dict[(channel, post_id, fid)] = ts` (обновление словаря на лупе — дёшево и атомарно в asyncio), и периодический flush-таск раз в 60 с: `executemany UPDATE` одним соединением.
- Flush также в shutdown (lifespan) — не терять хвост.
- Убрать fire-and-forget `asyncio.create_task(_update_access(...))` целиком.

### 5.2 Батч-апсерты media id

- Сделаны в 4.2 (`upsert_media_file_ids_bulk_sync`).

**Где не наебаться:**
- Flush-таск — под supervision из 1.4.
- Ключи как и раньше: channel — строкой (`str(channel)`), не смешивать str/int в ключах — иначе UPDATE не найдёт строку и timestamp тихо перестанет обновляться (файлы начнут выпадать из кэша через 20 дней при живом трафике).
- Порядок в тесте: хит → flush → значение `added` в БД обновилось.

### DoD стадии 5
На кэш-хит /media — ноль обращений к SQLite в горячем пути (кроме кэша MIME); фоновая запись раз в минуту.

---

## Стадия 6 — healthcheck и деплой

### 6.1 Лёгкий `/ping`

- Новый эндпоинт без токена, без TG RPC, без os.walk:
  ```python
  @app.get("/ping")
  async def ping():
      age = client.watchdog_last_ok_age()   # seconds since last successful probe, None if never
      healthy = client.client.is_connected and (age is None or age < unhealthy_threshold)
      return JSONResponse({"status": "ok" if healthy else "degraded", ...},
                          status_code=200 if healthy else 503)
  ```
- В TelegramClient добавить публичный метод возраста `_wd_last_ok_monotonic`.

### 6.2 Обновить docker-compose пример

- healthcheck → `curl -sf http://127.0.0.1:80/ping`, interval 5m, timeout 5s.
- Прод-компоуз (вне репозитория) обновить руками — отметить в отчёте деплоя.

**Где не наебаться:**
- Текущий healthcheck (`/rss/...?limit=1`, timeout 5 с) сам создаёт флаки: на холодном кэше генерация легко > 5 с → autoheal рестартует контейнер посреди докачек → битые temp-файлы. После перехода на /ping «живость TG» проверяет вотчдог, а healthcheck — только живость процесса/лупа. Не оставлять старый URL в проде.
- `/ping` не должен дергать `get_me()` — иначе вернули ту же проблему через другую дверь.

### DoD стадии 6
Во время зависшего TG RPC `/ping` отвечает мгновенно (503 degraded), контейнер не рестартится от медленного фида.

---

## Стадия 7 — сквозная верификация

1. Полный pytest (старые 174 + новые).
2. Ручные сценарии (локально, curl):
   - Range: `curl -H "Range: bytes=0-99" / "bytes=-100" / "bytes=999999999-"` → 206/206/416.
   - Параллельные запросы одного большого видео (fake slow client) → нет частичной отдачи.
   - Отключение клиента на середине стрима → нет утечки тасков/фд (смотреть логи и lsof).
   - Генерация фида на 100+ сообщений + параллельный /ping → ping < 100 мс.
3. Деплой на прод и наблюдение по существующим diag-логам:
   - `diag_semaphore_wait`, `diag_download_timing` — ожидание должно упасть;
   - `diag_sanitize_slow` — должен почти исчезнуть;
   - `watchdog: heartbeat` — продолжаются;
   - отсутствие 404-всплесков на /media в момент FloodWait (теперь 429).
4. Rollback-план: каждая стадия — отдельный коммит (или PR) → откатывается индивидуально.

---

## Сводка главных ловушек (checklist перед каждым ревью)

1. `wait_for` — вокруг RPC, не вокруг acquire гейта/семафора.
2. In-flight future: скачивание в detached task, `set_exception` + `pop` в finally, иначе вечно занятый ключ или зависшие ожидающие.
3. `_save_media_file_ids` внутри потока = молчаливая потеря записей (RuntimeError → except → лог). Сначала 4.2, потом 4.3.
4. Убирая sanitize-проходы — карта выходных точек; каждый выход имеет ровно один проход; XSS-тесты.
5. Перенос в поток ≠ ускорение (GIL): обязательно сокращать объём работы (lazy raw_message, один sanitize).
6. FileResponse: сначала зафиксировать текущую Range-семантику тестами, потом менять.
7. Переименование `.tmp.` → `.part.`: обновить regex свипера, старый паттерн оставить на переходный период.
8. `task_done()` только после успешного `get()`; `put_nowait` вместо `await put()` там, где ловится QueueFull.
9. Ключи SQLite: channel всегда `str(...)` — рассинхрон типов тихо ломает обновление timestamp.
10. Большие видео: touch mtime при отдаче, иначе свипер удалит файл под зрителем.
11. Venv ≠ прод: выровнять Kurigram до 2.2.22 до начала работ.
12. Ветка `fix/stability`; на `main` не коммитить.

## Порядок и зависимости

- Стадия 1 → деплой возможен сразу (низкий риск, убирает зависания).
- Стадия 2 → деплой вторым (убирает флаки статики). Зависит от 1.3 (FloodWait в воркере).
- Стадия 3 — независима, но лучше после 2 (меньше конфликтов в prepare_file_response/download-путях).
- Стадия 4 требует 4.2 строго до 4.3; 4.4 можно отдельным коммитом.
- Стадия 5 частично делается в 4.2; аккумулятор access-time — независим.
- Стадия 6 — в любой момент, но эффект от неё максимален после 1–2.
