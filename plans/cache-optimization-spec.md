# Спецификация: рефакторинг и оптимизация кешей pyrogram-bridge

Статус: **принята владельцем 2026-07-05. Адверсарное ревью — 4 раунда, возражений не осталось.**
История ревью — раздел 12. Реализация разбита на ишью в gitea (по PR-юнитам: A+B+F, C, D, E).

## 0. Контекст и цели

В проекте три слоя кеша:

1. **История сообщений и инфо о канале** — pickle-файлы в `data/tgcache/` (`tg_cache.py`).
2. **Медиафайлы** — файлы в `data/cache/<channel>/<post_id>/<file_unique_id>` + метаданные в SQLite `data/media_file_ids.db` (`api_server.py`, `file_io.py`).
3. **Runtime-структуры** — `_access_updates`, `_inflight`, `download_queue` в `api_server.py`.

Проблемы, которые закрывает спецификация (по результатам аудита):

| # | Проблема | Пакет |
|---|---|---|
| 1 | Pickle полных `Message` — формат привязан к версии pyrogram/kurigram, тихий дрейф схемы, opaque-формат | A |
| 2 | Строгое `cached_limit == limit` + разные limit у RSS (`limit*2`) и HTML (`limit`) → кеш истории фактически не работает | B |
| 3 | Один канал живёт под ключами разного регистра/формы записи (`Durov`/`durov`, `@name`/`name`) в трёх слоях → дубли кеша. Унификация ID↔username — сознательная НЕ-цель (см. раздел 8) | C |
| 4 | Два почти идентичных pickle-кеша в `tg_cache.py` (история/chatinfo) — дублирование кода | A |
| 5 | Legacy-мусор (`*_history.cache`, двойной pickle, `data/media_file_ids.json`); чистка `data/tgcache` вводится как стартовая + периодический age-sweep (мгновенной GC по событию не будет — файлы мёртвых каналов живут до 7 суток) | A, F |
| 6 | Свипер медиа-кеша: полный проход таблицы + `os.walk` каждые 60 с при политиках «20 дней»/«1 час» | D |
| 7 | Баг: чистка «мёртвых» строк SQLite выполняется только при `files_removed > 0` | D |
| 8 | Чтение MIME из SQLite (новое соединение + threadpool-hop) на каждом хите `/media` | E |
| 9 | Джиттер TTL перебрасывается на каждом чтении → недетерминированное поведение у границы TTL (усложняет рассуждения и тесты) | F |
| 10 | Очередь фоновых загрузок без дедупа — повторная постановка одного файла каждым проходом свипа | D |
| 11 | Путь медиа-кеша (`./data/cache` + join) собирается вручную в 7 местах | E |

## 1. Состав пакетов и порядок

| Пакет | Задания | Что | Зависимости | Файлы |
|---|---|---|---|---|
| **A** | 1–5 | Снапшот-словари вместо pickle + generic JSON-store + чистка legacy tgcache | — | `message_snapshot.py` (новый), `tg_cache.py`, `api_server.py` (1 строка), тесты |
| **B** | 6–7 | Префиксная выдача истории вместо строгого `limit` | после A | `tg_cache.py`, тесты |
| **C** | 8–11 | Единая канонизация ключа канала + one-shot миграция | **после A** (задание 9.1 канонизирует ключ внутри `_cache_file_path`, который создаётся заданием 2) | `channel_key.py` (новый), `tg_cache.py`, `post_parser.py`, `api_server.py` |
| **D** | 12–14 | Фиксы свипера медиа-кеша + дедуп очереди | независим | `api_server.py`, `config.py` |
| **E** | 15–16 | Хелпер путей медиа-кеша + in-memory MIME-кеш | независим | `api_server.py` |
| **F** | 17–18 | Джиттер TTL при записи + age-sweep tgcache + добить legacy | после A | `tg_cache.py`, `api_server.py` |

Рекомендуемая сборка: A → B → F одним PR (все правят `tg_cache.py`); C — отдельный PR (меняет генерацию URL и содержит миграцию); D и E — параллельно с любым.

---

## 2. Пакет A — кеш истории на извлечённых словарях

### 2.1. Принцип

Кеш истории перестаёт хранить pickle живых объектов pyrogram. При записи из `Message`
извлекается JSON-словарь по явному allowlist полей (снапшот); при чтении из него
восстанавливается лёгкий duck-typed объект `CachedMessage`, неотличимый для конвейера
рендеринга (`rss_generator.py`, `post_parser.py`) от настоящего `Message`.
**Конвейер не меняется вообще** — главный инвариант дизайна.

Выгоды: формат на диске отвязан от версии pyrogram/kurigram; версионирование схемы
(несовпадение → честный miss вместо тихого дрейфа); JSON читаем при отладке;
явный документированный контракт «от каких полей Message зависит рендер»
(сейчас он размазан по ~2000 строк); файл меньше и парсится быстрее полного графа объектов.
Отклонённая альтернатива — раздел 11.

### 2.2. Ключевые проектные решения

| # | Решение | Обоснование |
|---|---|---|
| Р1 | Новый модуль `message_snapshot.py`: `snapshot_message()` / `restore_message()` | Схема сериализации отделена от механики кеша; меняются независимо |
| Р2 | `text`/`caption` хранятся парой `{plain, html}`; `.html` вычисляется **при записи** через pyrogram `Str.html` | Конвертация entities→HTML требует машинерии pyrogram, доступной на живом объекте (`post_parser.py:674-675`). Проверено: `Str.html` работает офлайн, без клиента |
| Р3 | При восстановлении `text`/`caption` — `CachedStr(str)` с атрибутом `.html` | Потребители используют и строковые операции (`len`, `strip`, regex, `or`), и `.text.html`; str-подкласс покрывает всё. Это паттерн pyrogram `Str` |
| Р4 | `media` восстанавливается в настоящий `MessageMediaType[name]`; `KeyError` → `None` + warning | 31 место сравнивает с enum и использует его ключом dict (`post_parser.py:1004-1017`) |
| Р5 | `service` хранится и восстанавливается **строкой-именем** (`"PINNED_MESSAGE"`) | Все потребители — truthiness и `'X' in str(service)` (`rss_generator.py:112-121`, `post_parser.py:305`). Строка версионно-устойчива |
| Р6 | `date` — `isoformat()` туда, `fromisoformat()` обратно | Сохраняет naive/aware ровно как у pyrogram; `strftime`, `timestamp()`, сортировки работают |
| Р7 | Вложенные объекты восстанавливаются с **полным набором ключей схемы, None-дефолты** — «как живые». **Единственное исключение — `forward_origin`**: у него присутствие ключа зеркалит присутствие атрибута на исходном объекте | Живые pyrogram-объекты всегда имеют все атрибуты (ставятся в `__init__`, напр. `Reaction`: `emoji=None, custom_emoji_id=None, is_paid=None`) — код обращается к вложенным полям и напрямую (`rss_generator.py:131` — `message.chat.username` в except-хендлере; `post_parser.py:1087-1090` — `u.username`/`u.active` без getattr), omit-None дал бы там `AttributeError` → 500 на фид. А вот живые `MessageOrigin*`-классы имеют РАЗНЫЕ наборы атрибутов, и `_format_forward_info` ветвится по `hasattr` (Case 1–5, `post_parser.py:629-669`) — для forward_origin присутствие ключей семантично |
| Р8 | `CachedMessage` — мутабельный, полный набор top-level атрибутов с дефолтами `None`/`False` | `_reply_enrichment` присваивает `message.reply_to_message` (`rss_generator.py:574`); доступ к атрибутам не должен кидать `AttributeError`; `copy.deepcopy` (`rss_generator.py:43`) должен работать |
| Р9 | Файл: JSON `{"version", "timestamp", "limit", "messages"}`, имена `<key>.history.json` / `<key>.chatinfo.json`. Атомарная запись: **уникальный tmp `<path>.tmp.<uuid4().hex>`** → `os.replace`, в `finally` — удаление своего tmp (образец: `_download_atomic`, `api_server.py:460-479`) | Версия отсекает старые схемы; новые расширения игнорируют старые pickle; уникальный tmp исключает перемешивание байтов при конкурентных miss'ах одного канала (RSS+HTML параллельно пишут через `asyncio.to_thread`); rename чинит порчу при падении посреди записи |
| Р10 | TTL / джиттер / строгий `limit` в пакете A — **без изменений** | Behavior-neutral рефакторинг; limit меняет пакет B, джиттер — пакет F |
| Р11 | Миграции старых pickle нет: старые файлы = miss; стартовая чистка удаляет legacy | Кеш самовосстанавливается за ≤ TTL (история 8 ч; chatinfo 12 ч по умолчанию, настраивается `TG_CHAT_CACHE_TTL_HOURS`) |

### 2.3. Схема снапшота v1 (контракт)

Выведена из фактического потребления полей конвейером (инвентарь по `rss_generator.py`
и `post_parser.py`, включая `hasattr`-семантику и enum-сравнения) и сверена с типами
kurigram 2.2.23 (`.venv`).

```jsonc
{
  "id": 123,                          // message.id
  "date": "2026-07-05T12:00:00",      // isoformat or null
  "text":    {"plain": "...", "html": "..."},   // null if absent
  "caption": {"plain": "...", "html": "..."},   // null if absent
  "media": "PHOTO",                   // MessageMediaType.name or null
  "service": "PINNED_MESSAGE",        // MessageServiceType.name or null
  "media_group_id": 456,              // or null
  "views": 789,                       // or null
  "show_caption_above_media": false,
  "reply_to_message_id": 42,          // needed by _reply_enrichment
  "empty": false,
  "chat": {"id": -100123, "username": "durov", "title": "...",
           "usernames": [{"username": "x", "active": true}]},
  "sender_chat": {"id": 1, "title": "...", "username": "..."},
  "from_user": {"first_name": "...", "last_name": "...", "username": "..."},
  "forward_origin": {                 // ONLY keys present on the live object (hasattr semantics!)
      "type": "channel",
      "chat": {"id": 1, "title": "...", "username": "..."},
      "sender_user_name": "...",
      "sender_user": {"first_name": "...", "last_name": "...", "username": "..."},
      "chat_id": 1, "title": "..."
  },
  "reactions": [                      // from message.reactions.reactions; null if none
      {"emoji": "👍", "count": 5, "is_paid": null, "custom_emoji_id": null},
      // custom-emoji reaction: emoji is null, custom_emoji_id is a STRING (kurigram
      // builds it as str(document_id)) — restored with the FULL key set, like live objects
      // (live kurigram sets is_paid=None unless it is a paid reaction):
      {"emoji": null, "count": 2, "is_paid": null, "custom_emoji_id": "987..."}
  ],
  "poll": {"question": "...",         // plain string — FormattedText unwrapped at snapshot time
           "options": [{"text": "..."}]},   // text unwrapped to plain string; see 2.4.1
  "web_page": {"type": "...", "url": "...", "display_url": "...", "site_name": "...",
               "title": "...", "description": "...", "has_large_media": false,
               "photo": {"file_unique_id": "..."}},
  // media payloads — per-type allowlist:
  "photo":      {"file_unique_id": "..."},
  "video":      {"file_unique_id": "...", "file_size": 123},   // file_size: large-video rule
  "document":   {"file_unique_id": "...", "mime_type": "..."},  // mime_type: PDF branch
  "audio":      {"file_unique_id": "...", "mime_type": "..."},
  "voice":      {"file_unique_id": "...", "mime_type": "..."},
  "video_note": {"file_unique_id": "..."},
  "animation":  {"file_unique_id": "..."},
  "sticker":    {"file_unique_id": "...", "emoji": "...", "is_video": false}
}
```

### 2.4. Критические тонкости (обязательны к учёту)

1. **`poll.question` и КАЖДЫЙ `poll.options[].text` в kurigram 2.2.23 — всегда объекты
   `FormattedText`** (проверено: `poll.py:105,220`, `poll_option.py:72,84` в `.venv`), не строки.
   Снапшот обязан разворачивать оба в plain-строку правилом
   `v.text if hasattr(v, 'text') else str(v)` (правило корректно и для `Str`, и для голой
   строки). Если положить `FormattedText` в словарь как есть — `json.dump` кинет `TypeError`,
   `_save_history_to_cache` проглотит её, и **кеш молча перестанет сохраняться для любого
   канала с опросом в выборке**. Восстановление: `question` → str,
   `options` → namespace с `.text` (голая строка в options дала бы
   `getattr(option, 'text', '') == ''` → пустые опции, `post_parser.py:992`).
2. **Реакции восстанавливаются с полным набором ключей** (`emoji`, `custom_emoji_id`,
   `count`, `is_paid`; отсутствующие значения — `None`) — ровно как живой `Reaction`,
   у которого `__init__` всегда ставит все атрибуты. Ветвление в
   `post_parser.py:399-410` и `:928-934` на живых объектах работает через truthiness,
   а не через отсутствие атрибута — восстановленный объект обязан вести себя так же.
   `custom_emoji_id` — **строка** (kurigram: `str(reaction.document_id)`).
3. **`forward_origin`** — единственный объект с presence-семантикой: Case 4 различается по
   `hasattr(forward_origin, "chat_id") and hasattr(..., "title")`; при снапшоте пишутся
   только реально присутствующие (non-None) поля-кандидаты, при восстановлении — только
   записанные ключи.
4. **`video.file_size` обязателен** — правило «не кешировать видео >100 МБ» в
   `_save_media_file_ids` (`post_parser.py:1058`).
5. **`chat.usernames`** — список объектов с `.username`/`.active`
   (`post_parser.py:1087-1090`, прямой доступ без getattr — оба ключа всегда присутствуют).
6. **`CachedMessage.__str__`** — читаемый JSON-дамп: попадает в error-логи
   (`post_parser.py:980`).

### 2.5. Задание 1 — новый модуль `message_snapshot.py`

```python
SNAPSHOT_VERSION = 1

class CachedStr(str):
    """str subclass carrying a precomputed .html rendering, mirroring pyrogram's Str.
    deepcopy/pickle preserve the instance __dict__, so .html survives copying."""
    # factory: CachedStr.build(plain, html)

class CachedMessage:
    """Duck-typed stand-in for pyrogram Message, restored from a snapshot dict.
    Mutable (reply enrichment assigns .reply_to_message). All pipeline-consumed
    top-level attributes exist with None/False defaults, so getattr never raises."""
    # __str__/__repr__: json.dumps of the snapshot dict, default=str

def snapshot_message(message) -> dict: ...
def restore_message(data: dict) -> CachedMessage: ...
def snapshot_messages(messages) -> list[dict]: ...
def restore_messages(items: list[dict]) -> list[CachedMessage]: ...
```

Требования:
- `snapshot_message` извлекает строго по схеме 2.3; каждое поле через `getattr(..., None)`.
- Styled-text (`FormattedText`/`Str`/str) разворачивается правилом
  `v.text if hasattr(v, 'text') else str(v)` — применяется к `poll.question` и каждому
  `poll.options[].text` (см. 2.4.1).
- `text`/`caption`: `{"plain": str(value), "html": value.html}`; если `.html` недоступен —
  `html = plain`.
- `media`: `message.media.name`; `service`: `message.service.name`
  (оба через безопасный `getattr(x, 'name', str(x))`).
- Восстановление: помощник `_ns(d: dict, keys: tuple) -> SimpleNamespace` — ставит ВСЕ
  перечисленные ключи схемы (None-дефолт для отсутствующих); для `forward_origin` — режим
  «только записанные ключи». Спец-обработка: `text/caption → CachedStr`,
  `media → MessageMediaType[name]` (при `KeyError` — warning + `None`),
  `date → datetime.fromisoformat`, `reactions → SimpleNamespace(reactions=[...])`
  (обёртка-контейнер, как у pyrogram).
- Дефолты `CachedMessage`: `id, date, text, caption, media, service, media_group_id, views,
  show_caption_above_media, reply_to_message_id, reply_to_message=None, empty=False, chat,
  sender_chat, from_user, forward_origin, reactions, poll, web_page, photo, video, document,
  audio, voice, video_note, animation, sticker`.
- Комментарии в коде — только английские.

### 2.6. Задание 2 — переписать `tg_cache.py` на generic JSON-store

1. Убрать `import pickle` полностью. Удалить ветку double-pickle (`tg_cache.py:111-116`).
2. Generic-пара (заменяет обе дублирующиеся тройки функций):

```python
def _store_entry(path: str, payload: dict) -> None:
    """Atomically write {'version', 'timestamp', **payload} as JSON.
    Writes to a unique '<path>.tmp.<uuid4hex>' and os.replace()s it into place;
    the finally block always removes this writer's own tmp file."""

def _load_entry(path: str, max_age_hours: float) -> Optional[dict]:
    """Return payload dict, or None on: missing file, version mismatch,
    expired TTL (keep the existing up-to-20% read-time jitter as-is in package A;
    package F moves it to write time), JSON error."""
```

3. Пути: `<safe_key>.history.json` и `<safe_key>.chatinfo.json`
   (общий помощник `_cache_file_path(key, suffix)` вместо двух копий).
4. `_save_history_to_cache`: `payload = {'limit': limit, 'messages': snapshot_messages(messages)}`.
   `_get_history_from_cache`: проверка `limit` как сейчас (пакет A не меняет), возврат
   `restore_messages(...)`.
5. `_save_chat_to_cache` / `_get_chat_from_cache`: тот же store, `payload = {'data': {...}}`.
6. Новая функция `cleanup_legacy_cache_files() -> int` — удаляет из `CACHE_DIR` файлы
   `*.cache` и `*.chatinfo` (старые pickle-форматы, включая `*_history.cache`), возвращает
   число удалённых. Ошибки удаления — warning, не исключение.
7. Сигнатуры и поведение `cached_get_chat_history` / `cached_get_chat` не меняются:
   на miss возвращаются живые `Message`, на hit — `CachedMessage`; обе формы duck-совместимы
   (как сегодня pickle-копия vs живой объект).

### 2.7. Задание 3 — стартовая чистка в `api_server.py`

В `lifespan` после `init_db_sync`, **до** запуска фоновых задач:

```python
# One-shot cleanup of legacy pickle cache files (pre-JSON formats)
await asyncio.to_thread(cleanup_legacy_cache_files)
```

Больше в `api_server.py` в рамках пакета A ничего не трогать.

### 2.8. Задание 4 — тесты `tests/test_message_snapshot.py`

Фейковые `Message` — на `SimpleNamespace` (паттерн существующих тестов); `text` — мини-класс
`str` с атрибутом `.html`. Обязательные кейсы:

1. Round-trip основных полей: id, date (naive и aware — оба сохраняют tz-ность), text.html,
   caption.html, media enum (`is MessageMediaType.PHOTO`), views, media_group_id.
2. **Poll с FormattedText-подобными объектами**: фейк `question = SimpleNamespace(text="Q?",
   entities=[])`, каждый option — `SimpleNamespace(text=SimpleNamespace(text="Opt",
   entities=[]))`. Проверить: снапшот JSON-сериализуем (`json.dumps` не падает);
   восстановленный `poll.question` — строка; `getattr(option, 'text', '') == "Opt"`.
   Тест обязан ПАДАТЬ, если снапшот кладёт FormattedText как есть.
3. Реакции: обычная / paid / custom-emoji. У восстановленной custom-реакции:
   `hasattr(r, 'emoji') is True`, `r.emoji is None`, `r.custom_emoji_id` — строка;
   ветка в `_reactions_views_links`-подобной логике выбирается по truthiness, как у живой.
4. forward_origin Case 1–5: `hasattr`-ветвление выбирает правильный case
   (проверять именно наличие/отсутствие атрибутов — здесь presence-семантика сохраняется).
5. service: `'PINNED_MESSAGE' in str(restored.service)`.
6. Мутабельность + deepcopy: присвоение `msg.reply_to_message = X`;
   `copy.deepcopy(restored)` сохраняет `.text.html`.
7. Неизвестный media name (`"FUTURE_TYPE"`) → `media is None`, без исключения.
8. Store: `_store_entry`/`_load_entry` — round-trip, TTL-протухание, version mismatch → None,
   битый JSON → None; уникальность tmp-путей: перехватить `os.replace` (или замокать
   `uuid.uuid4`) и проверить, что два вызова `_store_entry` в один path использовали
   РАЗНЫЕ tmp-имена, а итоговый файл валиден — одиночный «конкурентный» прогон окно
   гонки не ловит и сломанную реализацию с фиксированным tmp пропустит;
   `cleanup_legacy_cache_files` удаляет `*.cache`/`*.chatinfo`, не трогает
   `*.history.json` (использовать `tmp_path`).
9. `_save_media_file_ids` на восстановленном сообщении с `video.file_size` > 100 МБ ничего
   не добавляет в `_pending_media_ids`.
10. Восстановленный `chat` для канала без username: `message.chat.username` — `None`
    (не `AttributeError`) — регресс-тест на правило Р7 (сценарий `rss_generator.py:131`).

### 2.9. Задание 5 — верификация пакета A

- `pytest` — весь существующий набор зелёный.
- `grep pickle tg_cache.py` — пусто; `post_parser.py` и `rss_generator.py` не изменены.

---

## 3. Пакет B — история: префикс вместо строгого равенства `limit`

**Проблема:** `tg_cache.py:106-109` требует `cached_limit == limit`; RSS просит `limit*2`
(`rss_generator.py:419`), HTML — `limit` (`rss_generator.py:639`), оба пишут в один файл.
Чередование запросов превращает кеш в решето.

### 3.1. Задание 6 — логика префикса в `_get_history_from_cache`

Сообщения в кеше лежат от новых к старым (порядок `get_chat_history`), поэтому запрос
с меньшим `limit` — это префикс:

```python
cached_limit = payload.get('limit', 0)
raw_messages = payload['messages']
# Serve from cache when it was fetched with an equal-or-larger limit, OR when the
# channel is exhausted (fewer messages exist than were asked for at fetch time —
# the cache then holds the channel's entire recent history and satisfies ANY limit).
if cached_limit < limit and len(raw_messages) >= cached_limit:
    log("history_cache_limit_insufficient: cached %s < requested %s", cached_limit, limit)
    return None
return restore_messages(raw_messages[:limit])
```

- `_save_history_to_cache` не меняется: хранит `limit`, с которым делался fetch.
- Лог хита дополнить: `served {limit} of cached {cached_limit}`.
- Сходимость: первый запрос с большим `limit` после протухания перезапишет кеш «широкой»
  версией, дальше меньшие запросы — хиты.

### 3.2. Задание 7 — тесты

1. Кеш `limit=100` (100 сообщений) → запрос `limit=50` — хит, ровно 50 первых, порядок сохранён.
2. Кеш `limit=50` → запрос `limit=100` — промах.
3. Исчерпанный канал: кеш `limit=100`, 37 сообщений → запрос `limit=200` — хит, 37 сообщений.
4. TTL работает независимо от limit.

---

## 4. Пакет C — единая канонизация ключа канала

**Проблема:** один канал живёт под ключами `Durov` / `durov` / `@name` в tgcache-файлах,
каталогах `data/cache/` и строках SQLite → дубли кеша, двойные походы в Telegram,
осиротевшие деревья при смене регистра/формы. (Унификация ID↔username — не-цель, раздел 8.)

### 4.1. Задание 8 — новый модуль `channel_key.py`

Отдельный модуль без зависимостей (его импортируют `tg_cache`, `post_parser`, `api_server` —
отдельность исключает циклические импорты):

```python
def canonical_channel_key(channel: str | int) -> str:
    """Canonical cache/DB key for a channel.

    Telegram usernames are case-insensitive -> lowercase them.
    Numeric '-100...' ids keep their exact string form.
    The '@' prefix is stripped.

    The canonical form is also SAFE to pass to Telegram API calls (usernames are
    case-insensitive on the API side; the numeric form is unchanged) — callers that
    thread one value through both filesystem paths and API calls may use it for both.
    """
    s = str(channel).strip().lstrip('@')
    if s.startswith('-100') and s[4:].isdigit():
        return s
    return s.lower()
```

### 4.2. Задание 9 — применить ключ во всех трёх слоях

1. **`tg_cache.py`**: в `_cache_file_path(key, suffix)` прогонять ключ через
   `canonical_channel_key`.
2. **`post_parser.get_channel_username`** (`post_parser.py:1081-1097`): возвращать username
   в нижнем регистре (обе ветки — `usernames`-список и одиночный `username`). Это
   канонизирует записи SQLite (`_save_media_file_ids`), генерируемые media-URL и `t.me`-ссылки
   (t.me к регистру нечувствителен).
3. **`api_server.get_media`** (`api_server.py:1153-1183`): после проверки digest вычислить
   `fs_channel = canonical_channel_key(channel)` и использовать вместо `str(channel)` везде
   ниже: pre-semaphore путь, ключ `_access_updates`, `media_key`, аргумент `_download_deduped`
   (протекает в `download_media_file` → каталоги, API-вызов и удаление из SQLite — для API
   канонический идентификатор безопасен, см. docstring 4.1).
   **Digest проверять по исходной строке URL** — иначе сломаются все выданные ссылки.

### 4.3. Задание 10 — one-shot миграция существующих данных

Без миграции каждый ранее закешированный медиафайл канала с не-lowercase username даёт
после деплоя cache-miss → повторную загрузку из Telegram (шторм ограничен такими каналами
и фактически запрашиваемыми файлами, но страховка дешёвая — делаем).

Новая функция `migrate_channel_keys_sync(db_path: str, cache_dir: str) -> None`. Вызов —
в `lifespan` **после `init_db_sync`, ДО `client.start()` и до запуска фоновых задач**,
строго через `await asyncio.to_thread(migrate_channel_keys_sync, ...)` — функция
синхронная (FS-rename + SQLite), голый вызов заблокировал бы event loop на время
миграции; размещение до `client.start()` дополнительно исключает влияние на сетевые
таски pyrogram и гонки со свипером/access-flush.

Единица работы — **канал целиком, сначала FS, потом SQL** (порядок принципиален:
при провале FS-части SQL-строки канала остаются старорегистровыми, и старое дерево
по-прежнему видно свиперу через строки БД — вечных orphan-каталогов не возникает):

1. Найти каналы-кандидаты: имена каталогов первого уровня в `cache_dir` с не-lowercase
   именем (кроме `-100...`) ∪ `SELECT DISTINCT channel ... WHERE channel != lower(channel)
   AND channel NOT LIKE '-100%'`.
2. Для каждого канала — **FS-шаг**:
   - **ОБЯЗАТЕЛЬНЫЙ guard до любых действий**: если `os.path.exists(dst)` и
     `os.path.samefile(src, dst)` — **no-op FS-шага, перейти к SQL-шагу**. На
     case-insensitive FS (macOS/APFS, Docker Desktop for Mac — том `./data` наследует
     семантику хоста) `Durov/` и `durov/` — один и тот же каталог; merge без guard'а
     удалил бы весь кеш канала.
   - `dst` не существует → `os.rename(src, dst)`.
   - `dst` существует и это другой каталог (case-sensitive FS, обе формы реально есть) →
     пофайловый merge: существующий файл в `dst` выигрывает; затем удалить пустые
     остатки `src`.
   - FS-шаг упал (EACCES и т.п.) → log error с пометкой orphan-кандидата, **SQL-шаг
     канала пропустить**, перейти к следующему каналу.
3. **SQL-шаг** (только после успешного FS-шага): для каждой строки канала:
   - lowercase-строки с тем же `(post_id, file_unique_id)` нет — просто `UPDATE` канала;
   - есть — merge: `added = max(added)` из двух, `mime_type` — предпочесть non-NULL;
     затем `DELETE` старорегистровой строки. Голый `UPDATE ... SET channel=lower(channel)`
     запрещён — ловит PK-конфликт при наличии обеих форм.
4. Ошибки миграции — log error + продолжить работу (миграция не должна уронить старт);
   повторный запуск функции — идемпотентный no-op.
5. **Итоговая сводка одной строкой лога** (сигнал оператору, что деплой C прошёл штатно):
   `migration_summary: rows merged N, dirs renamed M, samefile no-ops K, failures F`.

### 4.4. Задание 11 — тесты + заметка о миграции

- Юнит: `'Durov'→'durov'`, `'@durov'→'durov'`, `-1001…` (int и str) → `'-1001…'`.
- Интеграция: путь tgcache-файла одинаков для `Durov`/`durov`; `get_channel_username`
  на фейковом chat с `username='MixedCase'` → `'mixedcase'`.
- Миграция: (а) SQL-merge обеих форм — остаётся одна строка с `max(added)` и non-NULL
  `mime_type`; (б) guard: вызов с `src`/`dst`, резолвящимися в один каталог (samefile), —
  no-op, данные целы; (в) merge на двух реально разных каталогах — файлы объединены,
  target выигрывает; (г) повторный запуск — no-op.
- В PR: (а) остаточные старорегистровые строки/каталоги, не покрытые миграцией (например,
  появившиеся между бэкапом и деплоем), доживают до 20-дневного вытеснения сами;
  (б) **миграция односторонняя** — reverse-миграции нет; откат пакета C после её
  выполнения возвращает код со старорегистровыми ключами на lowercase-данные и повторяет
  re-download-сценарий зеркально (ограниченный, самоизлечивается за 20 дней) — это
  ожидаемый признак отката, не авария.

---

## 5. Пакет D — свипер медиа-кеша

### 5.1. Задание 12 — фикс чистки БД (баг)

`api_server.py:820-836`: diff и удаление из SQLite выполняются только `if files_removed > 0`.
Запись старше 20 дней **без файла на диске** выпадает из списка без инкремента счётчика
(`api_server.py:663-687`) — и остаётся в БД навсегда, если в том же проходе не удалился
ни один реальный файл. Исправление:

```python
# Compute the DB diff unconditionally: entries dropped from the list because the
# file was already gone must still be purged from SQLite.
updated_set = {...}
removed_entries = [...]
if removed_entries:
    await asyncio.to_thread(remove_media_file_ids_sync, DB_PATH, removed_entries)
    logger.info(f"cache_sweep: purged {len(removed_entries)} entries "
                f"({files_removed} files removed from disk)")
```

### 5.2. Задание 13 — интервал свипа в конфиг

Сейчас `delay = 60` (`api_server.py:811`). Добавить в `config.py` по существующему паттерну
ключ `cache_sweep_interval` (env `CACHE_SWEEP_INTERVAL`, дефолт `900`, минимум `60`),
использовать в `cache_media_files`.

### 5.3. Задание 14 — дедуп очереди фоновых загрузок

Module-level `_queued_media: set[tuple[str, int, str]]`:
- в `download_new_files`: ключ в set'е → skip; иначе `add` + `put_nowait`
  (при `QueueFull` — `discard` и `break` как сейчас);
- в `background_download_worker`: в `finally` рядом с `task_done()` — `discard(key)`.

Event loop однопоточный, обе точки — корутины без await между проверкой и мутацией → гонок нет.
Тест: два подряд вызова `download_new_files` с одним списком кладут в очередь один элемент.

---

## 6. Пакет E — горячий путь `/media`

### 6.1. Задание 15 — константа и хелпер путей

`os.path.abspath("./data/cache")` + ручной `join` собираются в **семи** местах
(`api_server.py:207, 532-536, 667, 757-766, 817, 851, 1172-1175`). Ввести на уровне модуля:

```python
MEDIA_CACHE_DIR = os.path.abspath(os.path.join("data", "cache"))  # resolved once at import

def media_cache_path(channel: str, post_id: int, file_unique_id: str | None = None) -> str:
    """Single source of truth for the on-disk layout: <root>/<channel>/<post_id>[/<fid>]."""
```

Заменить все семь мест. `remove_old_cached_files_sync`/`download_new_files` сохраняют
параметр `cache_dir` (тестируемость), вызыватели передают константу.

### 6.2. Задание 16 — in-memory кеш MIME

Запись access-time уже вынесена с горячего пути в аккумулятор, а чтение MIME до сих пор
делает threadpool-hop + новое SQLite-соединение на каждый хит (`api_server.py:389-404`).
MIME по ключу `file_unique_id` иммутабелен.

```python
# MIME types are immutable per file_unique_id, so a process-lifetime dict in front of
# SQLite removes a to_thread + connect from every cache-hit response.
_mime_types: dict[tuple[str, int, str], str] = {}
_MIME_CACHE_MAX = 50_000  # crude bound; clear-all on overflow is fine at this size
```

Порядок в `prepare_file_response`: dict → (miss) SQLite → (miss) python-magic → записать
и в SQLite, и в dict. При успешном чтении из SQLite — заполнить dict. При переполнении —
`clear()`. Тест: замокать `get_mime_type_sync`, два запроса подряд — второй его не вызывает.

---

## 7. Пакет F — довесок к новому store

### 7.1. Задание 17 — джиттер TTL при записи

Сейчас `random_factor` перебрасывается на каждом чтении (`tg_cache.py:98, 192`) — срок жизни
записи недетерминирован, что усложняет рассуждения о поведении и тесты (одна и та же запись
у границы TTL может дать разный результат в соседних чтениях; устойчивого «мигания» нет —
первый же miss ведёт к перезаписи со свежим timestamp). В generic-store пакета A:
- `_store_entry` дополнительно пишет `'jitter': random.uniform(0.8, 1.0)`;
- `_load_entry` считает `adjusted_max_age = max_age_hours * 3600 * entry.get('jitter', 1.0)`
  и не дёргает `random` при чтении.

Срок жизни записи детерминирован после записи, разброс между инстансами/каналами сохраняется.
Тест: одна и та же запись около границы TTL даёт стабильный результат при повторных чтениях.

### 7.2. Задание 18 — age-sweep tgcache + добить legacy-данные

1. Новая функция `sweep_tgcache(max_age_days: int = 7) -> int` в `tg_cache.py`: удаляет из
   `CACHE_DIR` любые файлы с mtime старше порога. Живые файлы перезаписываются каждые ≤12 ч,
   поэтому mtime > 7 суток — гарантированно мёртвый файл (умерший/переименованный канал,
   неканоничный ключ, осиротевший uuid-tmp от упавшего писателя). Гонка с писателем
   ВОЗМОЖНА (stat→unlink не атомарен против `os.replace`: sweep может удалить файл,
   обновлённый между stat и unlink), но безвредна — худший исход один лишний miss-refetch;
   гонка с читателем так же самоизлечивается как miss. Не выдавать это за инвариант
   атомарности в комментариях кода.
2. Вызовы: (а) один раз при старте — рядом с `cleanup_legacy_cache_files` (задание 3);
   (б) из цикла `cache_media_files` раз в проход, **обязательно через `asyncio.to_thread`**
   (listdir+stat+unlink — синхронный FS I/O; инвариант кодовой базы — ничего блокирующего
   на event loop). Каталог плоский, стоимость — миллисекунды.
3. Стартовая legacy-чистка (задание 3) остаётся: sweep покрывает legacy-файлы сам, но
   с задержкой до 7 суток; чистка по расширениям делает это мгновенно.
4. Расширить стартовую чистку: удалить `data/media_file_ids.json` (наследие до-SQLite
   хранилища, кодом не читается — проверено grep). Лог — одной строкой с перечнем удалённого.

---

## 8. Границы (сознательно НЕ входит)

- **Унификация ID↔username** (`/rss/-100…` и `/rss/durov` одного канала остаются разными
  ключами во всех слоях): потребовала бы резолва через `get_chat` (лишний RPC, свои отказы
  и кеш-инвалидация). Канонизация пакета C устраняет только дубли регистра/формы записи.
- Переезд метаданных медиа с SQLite куда-либо ещё — SQLite остаётся.
- Кеширование результатов рендеринга (processed dicts / готовый HTML фида) — отдельная
  большая тема с инвалидацией по параметрам запроса.
- Оптимизация `calculate_cache_stats` (полный walk на `/health`) — низкий приоритет.
- `SimpleNamespace` → dataclass в `cached_get_chat` — косметика.

## 9. Сквозные критерии приёмки

1. `pytest` зелёный; `post_parser.py`/`rss_generator.py` изменены только в объёме
   задания 9.2 (lowercase в `get_channel_username`).
2. `grep pickle tg_cache.py` — пусто.
3. Сценарий «RSS(limit=100) → HTML(limit=50) → RSS(100)» порождает **один** поход в Telegram
   (сейчас — три).
4. `/rss/Durov` и `/rss/durov` используют один tgcache-файл; `/media/Durov/...` и
   `/media/durov/...` — один файл на диске.
5. Повторный `/media`-хит не создаёт SQLite-соединений (ни на чтение MIME, ни на запись
   access-time). Рецепт проверки: monkeypatch `file_io._open_db` счётчиком соединений;
   два последовательных запроса TestClient к одному файлу — на втором счётчик не растёт.
6. Строка SQLite с `added` старше 20 дней и отсутствующим файлом исчезает из БД за один
   проход свипа.
7. Кеш истории сохраняется и для каналов с опросами в выборке (снапшот JSON-сериализуем).
8. Миграция ключей на case-insensitive FS — no-op без потери данных (samefile-guard).

## 10. Риски

| Риск | Закрытие / статус |
|---|---|
| Пропущенное поле allowlist → тихая деградация рендера | **Живой риск, не закрыт полностью.** Инвентарь снят по всем потребителям (2.3–2.4) и сверен с kurigram 2.2.23; ревью нашло и закрыло два пропуска (FormattedText в poll, omit-None семантика) — но гарантии полноты ручного инвентаря нет. Митигция: тесты 2.8, деградация проявляется как выпадение конкретного элемента рендера, не крэш; TTL 8 ч ограничивает окно |
| Смешение живых `Message` (miss) и `CachedMessage` (hit) | Так работает и текущий pickle-кеш; живой объект — надмножество контракта |
| Порча файла при падении посреди записи / конкурентная запись | Уникальный tmp + `os.replace` + finally-очистка (Р9) |
| Старые pickle после деплоя | Игнорируются новыми именами + `cleanup_legacy_cache_files()` |
| Канонизация: существующие данные со старым регистром | One-shot миграция (задание 10) с samefile-guard; остатки доживают до 20-дневного LRU |
| Миграция на case-insensitive FS | Обязательный `os.path.samefile`-guard → no-op (критерий 9.8) |

## 11. Рассмотренные альтернативы

**Version-stamped pickle** (минимальный вариант): оставить pickle, дописать в конверт
`pyrogram.__version__` + версию схемы, несовпадение считать miss'ом (~5 строк). Закрывает
«тихий дрейф» и стоимость апгрейда библиотеки (один TTL-эквивалентный miss на канал —
пренебрежимо при TTL 8 ч). **Отклонена решением владельца проекта** (альтернатива
предлагалась явно на этапе аудита как «рекомендация-минимум»; выбран стратегический
вариант) по причинам: (а) исходная цель — «проще и поддерживаемее»: JSON читаем глазами
при отладке, pickle — нет; (б) снапшот делает зависимость рендера от полей `Message`
явным документированным контрактом (схема 2.3) вместо размазанной по ~2000 строк;
(в) меньший файл и дешёвый parse вместо unpickle графа объектов в threadpool.
Цена: ручной инвентарь полей с остаточным риском пропуска (раздел 10, первый пункт).

## 12. История ревью

Спецификация прошла два раунда адверсарного ревью (субагент-критик с доступом к кодовой
базе и установленному kurigram 2.2.23). Итоги:

- **C1 (BLOCKER, принято)**: исходная схема не разворачивала `FormattedText` в
  `poll.options[].text` → `json.dump` падал бы, кеш молча переставал сохраняться для
  каналов с опросами; исходные тест-фикстуры дефект не ловили. Исправлено: 2.4.1, тест 2.8.2.
- **C2 (MAJOR, принято с усилением)**: blanket omit-None ломал эквивалентность с живыми
  объектами (у которых все атрибуты всегда присутствуют) и давал крэш-пути
  (`rss_generator.py:131`, `post_parser.py:1087`). Итог: правило Р7 «полные ключи, кроме
  forward_origin».
- **C3 (MAJOR, принято)**: канонизация без миграции → повторные загрузки медиа после деплоя.
  Добавлено задание 10. Встречное возражение критика к первой версии миграции
  (**потеря кеша на case-insensitive FS**) принято: обязательный samefile-guard.
- **C4 (MAJOR → MINOR)**: критик требовал обосновать отказ от version-stamped pickle.
  Закрыто разделом 11; довод «холодный кеш на каждый бамп библиотеки» в обоснование
  сознательно НЕ включён (стоимость — один miss на канал, пренебрежимо). Остаточный MINOR:
  риск ручного инвентаря остаётся живым (раздел 10).
- **C5–C12 (MINOR, приняты)**: уникальный tmp; docstring `canonical_channel_key`;
  сужение проблемы #3; седьмое место сборки пути (`:817`); TTL 8–12 ч; `custom_emoji_id: str`;
  честная мотивация джиттера; age-sweep tgcache (+`to_thread` из фонового цикла).
- **Раунд 4 (новые векторы: атака на собственные правки критика, межпакетные
  взаимодействия, реализуемость, рантайм-семантика, операционка): 6 MINOR, все приняты.**
  D1 — `to_thread` + размещение миграции до `client.start()`; D2 — порядок «FS → SQL
  per-channel», чтобы частичный отказ FS не рождал orphan-деревья, невидимые свиперу;
  D3 — гонка sweep/писатель честно названа возможной-но-безвредной; D4 — зависимость
  «C после A» зафиксирована в таблице (задание 9.1 опирается на `_cache_file_path` из
  задания 2); D5 — тест 2.8.8 переписан на дискриминирующий (уникальность tmp-путей),
  критерий 9.5 получил рецепт проверки; D6 — итоговая сводка миграции в лог + пометка
  об односторонности миграции при откате. По направлениям «откаты A/B/F», «смешение
  живых и восстановленных объектов», «json.dumps(CachedStr)», «deepcopy», «exclude_*»
  критик существенных находок не нашёл (с доказательствами по коду: hit/miss атомарен
  на весь список, `/json`-эндпоинт идёт мимо кеша истории, tz-гомогенность гарантирована Р6).
