# Рефакторинг пайплайна рендера: спецификация (v6)

Статус: пять раундов адверсариального ревью — четыре у постоянного критика +
независимый холодный проход (журнал в §8), блокеров нет. Холодный проход
независимо подтвердил все line-refs и эмпирические утверждения спеки.
v6: golden-корпус переведён с синтетики на записанные реальные сообщения
(решение владельца, обоснование в этапе 0 и §8).
База (обновлено 2026-07-05, после мёржа PR #21): ветка
`refactor/render-pipeline` режется от **актуального `main`** (merge-коммит
`88ac436`+), НЕ от голого f9550d8 — main теперь содержит kurigram-код
(f9550d8) + review-fix `6388f2f` + саму эту спеку и корпус фикстур
(коммиты docs). Все line-refs спеки по-прежнему действительны: `6388f2f` —
чисто тестовый (только `tests/test_new_media_types.py`), source
post_parser.py / rss_generator.py / api_server.py не сдвинулся ни на строку
относительно f9550d8. Предусловие «kurigram в main» — ВЫПОЛНЕНО.
Ишью: эпик [#34](https://gitea.vvzvlad.xyz/vvzvlad/pyrogram-bridge/issues/34),
этапы 0–6 → [#27](https://gitea.vvzvlad.xyz/vvzvlad/pyrogram-bridge/issues/27),
[#28](https://gitea.vvzvlad.xyz/vvzvlad/pyrogram-bridge/issues/28),
[#29](https://gitea.vvzvlad.xyz/vvzvlad/pyrogram-bridge/issues/29),
[#30](https://gitea.vvzvlad.xyz/vvzvlad/pyrogram-bridge/issues/30),
[#31](https://gitea.vvzvlad.xyz/vvzvlad/pyrogram-bridge/issues/31),
[#32](https://gitea.vvzvlad.xyz/vvzvlad/pyrogram-bridge/issues/32),
[#33](https://gitea.vvzvlad.xyz/vvzvlad/pyrogram-bridge/issues/33).
Устаревшее [#22](https://gitea.vvzvlad.xyz/vvzvlad/pyrogram-bridge/issues/22)
(этап 1 по v1) закрыто как superseded.

## 1. Проблема

Рендер фидов (RSS/HTML) — не однонаправленный пайплайн, а набор циклов с
обратными ходами и дублированием:

- `generate_channel_rss` и `generate_channel_html` — близнецы на ~80%
  (rss_generator.py:347–527 и 578–717).
- Обратный ход данных: `processed_message_to_tg_message`
  (rss_generator.py:156–201) конвертирует отрендеренный dict обратно в
  мок-Message ради повторного рендера футера — при живых `Message` в `group`.
- Санитайз-конфиг скопирован трижды (post_parser.py:702, rss_generator.py:468,
  679) и разъехался: `s`/`del` разрешены только в post_parser.
- RSS-путь: отдельный `asyncio.to_thread` + новый `CSSSanitizer` + вложенная
  функция НА КАЖДЫЙ пост — при том, что `_render_pipeline` уже в worker-треде.
- `_create_time_based_media_groups`: `copy.deepcopy` всех сообщений на каждый
  запрос (rss_generator.py:43) ради защиты кэша от мутации `media_group_id`.
- post_parser: три параллельные лестницы выбора media-объекта
  (`_get_file_unique_id`, `_save_media_file_ids`, `_generate_html_media`),
  синхронизируемые вручную.

Латентные баги, найденные при анализе и ревью:

- `<hr class="post-divider">` добавляется ДО финального санитайза, `hr` нет в
  whitelist → bleach вырезает все разделители HTML-фида.
- `_sanitize_html` при исключении bleach возвращает сырой HTML (fail-open) —
  потенциальный stored-XSS; фидовые копии fail-closed.
- `_generate_html_media`: при `file_unique_id is None` (в т.ч. массовый случай
  WEB_PAGE без фото) открытый `<div class="message-media">` не закрывается;
  при whole-feed санитайзе html5lib «заглатывает» в него все последующие посты.
- **Naive/aware краш в дефолтном пути**: kurigram-даты naive-local
  (`datetime.fromtimestamp`), фолбэк в sort-ключе групп — aware UTC
  (rss_generator.py:141). Один None-date пост в фиде с реальными датами даёт
  TypeError → 500 при ЛЮБОМ `time_based_merge` (проверено эмпирически);
  при `time_based_merge=True` тот же класс краша дополнительно в
  тайм-кластеризации (строки 45–46, 61–62). Тесты слепы: мокают aware-даты.
- FloodWait из `cached_get_chat_history` перехватывается `except Exception` и
  превращается в ValueError → HTTP 400 вместо 429 (rss_generator.py:416–422,
  636–642; маппинг ValueError→400 — api_server.py:1334–1337).
- `_reactions_views_links`: reactions-объект с пустым списком реакций даёт
  `first_line_parts.append("")` → ведущий разделитель `…|…` в футере
  (post_parser.py:1081–1104, branch).

## 2. Целевая архитектура

```text
                    ┌───────────── async-зона (event loop) ─────────────┐
api_server ──► generate_channel_rss ─┐
                                     ├─► _prepare_feed_posts(...) ──► PreparedFeed
api_server ──► generate_channel_html ┘        │
                                              │  fetch (cached_get_chat / history)
                                              │  enrich (_reply_enrichment, опция)
                                              ▼
                    ┌───────── to_thread: _render_pipeline (sync) ──────┐
                    │ сортировка date-ASC (naive-safe) при time_merge    │
                    │ _compute_time_based_group_ids (чистая, без мутаций)│
                    │ _create_messages_groups(msgs, group_ids)           │
                    │ [:limit] → _render_messages_groups                 │
                    │            (рендер → фильтры → sort — внутри неё)  │
                    │ затем sanitize_html на каждый ОТФИЛЬТРОВАННЫЙ пост │
                    └────────────────────────────────────── санитайз ───┘
                                              │
                     RSS: feedgen из готовых постов     HTML: '\n<hr>\n'.join(...)
```

Решения:

1. Санитайз внутри `_render_pipeline`, per-post, ПОСЛЕ фильтров (не тратить
   bleach на отфильтровываемые посты).
2. Один модуль `sanitizer.py` — единственный источник конфига bleach, включая
   `protocols=['http','https','tg']` и `strip=True` (оба отличаются от
   дефолтов bleach!).
3. Футер merged-групп из настоящего main-сообщения; конвертер удаляется.
4. Группировка — чистая функция: маппинг `msg.id → effective_group_id`;
   кэшированные Message пайплайном не мутируются.
5. Общий препроцессинг `_prepare_feed_posts`; различия путей — явные параметры.
6. Единая таблица медиа-типов в post_parser (селектор + renderer на kind).

Одиночный пост (`get_post` → `process_message(sanitize=True)` → `_format_html`)
не меняется, за исключением реестра §3 пп. 2, 13, 14, 15 (fail-closed,
guard >100 МБ, закрытие div, пустые реакции — эти механизмы общие с фидовым
путём).

## 3. Реестр намеренных изменений поведения

Всё, что не перечислено здесь, обязано остаться бит-в-бит идентичным.
Golden-эталоны (§5, этап 0) обновляются только коммитом со ссылкой на пункт
этого реестра.

| № | Изменение | Тип | Этап |
| --- | --------- | --- | ---- |
| 1 | `s`, `del` разрешены и в фидах | багфикс | 1 |
| 2 | `sanitize_html`: fail-open → fail-closed (`html.escape`); затрагивает и single-post/JSON путь | security | 1 |
| 3 | `<hr class="post-divider">` реально виден в HTML-фиде | багфикс | 1 |
| 4 | Несбалансированный HTML-фрагмент нормализуется в границах СВОЕГО поста и не искажает DOM последующих (только HTML-фид; RSS уже per-post и не меняется) | багфикс | 1 |
| 5 | Fail-closed гранулярность HTML-фида: эскейпится упавший пост, а не весь фид | улучшение | 1 |
| 6 | Merged-футер: custom-emoji реакции больше не агрегируются в один «❓ N» — отдельный span на каждую, как у одиночных постов | унификация | 2 |
| 7 | Merged-футер: дата печатается из naive-local даты реального Message вместо UTC-мока — на серверах с TZ≠UTC видимый сдвиг. Golden (TZ=UTC) эту дельту НЕ видит — верифицируется выделенным тестом с не-UTC TZ | унификация | 2 |
| 8 | Порядок флагов merged-поста детерминирован (first-seen order) | детерминизм | 2 |
| 9 | FloodWait при получении истории пробрасывается → HTTP 429 (было: ValueError → 400) | фикс HTTP | 3 |
| 10 | Тексты `detail` в 400-ответах унифицируются (исчезает суффикс «in HTML generation») | минорное | 3 |
| 11 | None-date сообщения исключаются из ТАЙМ-кластеризации (не получают entry в маппинге; их собственный `media_group_id` продолжает действовать). Было: смешанный naive+None вход (прод-даты kurigram) ронял фид TypeError'ом; полностью-None и aware+None входы (возникают только в тестах с aware-моками) выживали и кластеризовали None-date хвост по порядку вставки, включая adoption truthy id. ВСЕ эти поведения заменяются; новое поведение закрепляется отдельными тестами как сознательное | багфикс | 4 |
| 12 | Sort-ключи naive-безопасны (timestamp-based): None-date больше не роняет сортировку групп (краш жил в ДЕФОЛТНОМ пути, см. §1). Позиция None-date групп: фолбэк `+inf` в сортировке ГРУПП — детерминированно переживают `[:limit]`-срез как новейшие (теоретическая дельта: при постах с датой в будущем старый aware-путь ставил None-date «на сейчас», т.е. ниже них; прод-naive путь всё равно падал); позиция в итоговом выводе не меняется — существующий фолбэк `0.0` финальной сортировки постов ставит их в конец фида | багфикс | 4 |
| 13 | Правило «>100 МБ не кэшируем» применяется к любому медиа-объекту, не только `message.video` | унификация | 5b |
| 14 | Незакрытый `<div class="message-media">` закрывается во всех ветках | багфикс | 5b |
| 15 | Пустой reactions-объект больше не даёт ведущий разделитель в футере (`_reactions_views_links` не добавляет пустую строку); затрагивает и одиночные посты | багфикс | 2 |
| 16 | Логи ошибок санитайза объединяются под ОДНИМ именем `html_sanitization_error` + log_context (было три grep-имени: `html_sanitization_error` / `rss_html_sanitization_error` / `html_final_sanitization_error`) — правила grep-мониторинга обновить при деплое | наблюдаемость | 1 |

## 4. Общие правила всех этапов

- База: `refactor/render-pipeline` от актуального main (88ac436+, содержит
  f9550d8 + спеку + корпус). Один этап = один коммит
  (этап 5 — два: 5a/5b); после каждого — `pytest` полностью зелёный.
- Правки существующих тестов допустимы в двух случаях: (а) тест закрепляет
  старое поведение из реестра §3 — правка с комментарием-ссылкой на номер
  пункта; (б) чистое перемещение API (импорты, monkeypatch-цели) — правка с
  пометкой «API relocation, no behavior change».
- Два слоя эталонов, не смешивать: **feed-level** golden-снапшоты (этап 0,
  санитайженный выход RSS/HTML) и **fragment-level** снапшоты
  `_generate_html_media` (этап 5a, до санитайза). Этап 5b не «чинит» то,
  что уже изменил этап 1 на feed-уровне.
- Комментарии в коде — только на английском. Точечные правки. Устаревшие
  комментарии («4.4 coverage map» и т.п.) обновлять по ходу.
- Наблюдаемость: имена существующих лог-строк и их контекст (счётчики
  сообщений, `rss_date_range`, channel/message_id в ошибках санитайза)
  сохраняются — grep-мониторинг не должен ослепнуть. Единственное
  зарегистрированное исключение — объединение трёх имён санитайз-ошибок
  (§3.16).
- Новые тесты используют **naive**-даты (как отдаёт kurigram на проде), а не
  aware-UTC.

## 5. Этапы

### Этап 0 — golden-эталоны фидов (до любых правок кода)

**Корпус — записанные РЕАЛЬНЫЕ сообщения с прод-сервера, не синтетика (v6).**
Источник: рабочий кэш бриджа `data/tgcache/` на сервере — там уже лежат
пиклы ровно нужного формата: `{channel}.cache` =
`{'timestamp', 'limit', 'messages': List[Message]}`
(`tg_cache._save_history_to_cache`) и `{channel}.chatinfo` (для
`cached_get_chat`). Отобранные ПАРЫ файлов лежат в
`tests/test_data/recorded/` и коммитятся как замороженный корпус (контент
этих публичных каналов попадает в репо).

Реплей: тестовый загрузчик распикливает файлы напрямую (`timestamp`/`limit`
игнорируются — никакой проверки свежести) и monkeypatch'ит
`tg_cache.cached_get_chat_history` / `cached_get_chat` на возврат записанных
объектов. Это буквально прод-путь cache-hit: сериализация и структура
объектов — те же, что видит рендер в бою.

Почему реальные, а не моки: (а) настоящие kurigram-объекты закрывают класс
ложной зелени «мок и код согласованно ждут атрибут, которого нет у реального
объекта» — холодный проход (§8, раунд 5) пометил его как неловимый на моках;
(б) naive-даты, реальные entities/reactions/webpage-структуры и комбинации
форматирования достаются бесплатно.

**Первое действие этапа — проверка совместимости пиклов**: кэш сервера
записан прод-версией kurigram, корпус распикливается под 2.2.23 (база
рефакторинга f9550d8). ВЫПОЛНЕНО — см. «Статус» ниже: 90/90 + 89/89 без
ошибок, фолбэк не понадобился.

**Инвентаризация покрытия**: мини-скрипт проходит по корпусу и печатает
media-типы, наличие media_groups / time-кластеров / forward / reply /
реакций (обычные, custom, paid, пустой объект) / webpage с фото и без /
зачёркивания (`<s>`/`<del>` в entities). Дыры покрытия закрываются
СИНТЕТИЧЕСКОЙ добавкой — только для недостижимого в записанном.

**Статус (2026-07-05): снапшот, проверка, инвентарь И ОТБОР уже выполнены.**
Снапшот прод-кэша: `data/tgcache_prod_2026-07-05/` (90 `.cache` +
89 `.chatinfo`, 53 МБ, вне git — `data/` в .gitignore). Отбор сделан жадно
по матрице признаков, 4 канала (2.7 МБ) уже лежат в
`tests/test_data/recorded/` (untracked; коммитятся первым коммитом этапа 0):

- `bladerunnerblues` — единственный источник GIVEAWAY + GIVEAWAY_WINNERS;
  плюс POLL, pdf, DOCUMENT, ANIMATION, r_paid (71), wp_nophoto;
- `theyforcedme` — вся редкая медиа-палитра разом: STICKER, AUDIO, VOICE,
  VIDEO_NOTE, POLL, ANIMATION; богат forward (13) и reply (10);
- `embedoka` — r_empty + r_custom (35) + strike (7) + POLL + pdf +
  wp_nophoto; forward 20, reply 14;
- `meow_design` — 51 time-кластерная пара (ядро для time_based_merge),
  media_groups 22, pdf, POLL, strike.

Суммарно закрыта ВСЯ матрица: PHOTO 246, VIDEO 40, media_groups 66,
forward 35, reply 27, wp_photo 11 + все редкие признаки выше.
Совместимость подтверждена: ВСЕ файлы распикливаются под kurigram
2.2.23 без ошибок. Инвентарь (8582 сообщения): PHOTO 5397, text-only 1609,
WEB_PAGE 764 (с фото 581 / без 183), VIDEO 653, DOCUMENT 51, ANIMATION 33,
POLL 31, VIDEO_NOTE 15, STICKER 13, AUDIO 8, VOICE 6, GIVEAWAY и
GIVEAWAY_WINNERS по 1; media_group-членов 3159, forward 670, reply 357,
time-кластерных пар (gap≤5s) ~294; реакции: обычных 17981, custom 1018,
paid 593, ПУСТЫХ reactions-объектов 17 (§3.15-edge покрыт реальными
данными!); зачёркиваний 150 (§3.1 покрыт); все 8582 даты naive (посылка
спеки подтверждена); None-date — 0. Синтетика нужна ТОЛЬКО для: None-date
(этап 4) и отсутствующих в корпусе типов — PAID_MEDIA, STORY, LIVE_PHOTO,
CHECKLIST, CONTACT/LOCATION/VENUE/DICE/GAME/INVOICE/UNSUPPORTED
(fragment-уровень этапа 5a).

Сценарии exclude_flags/exclude_text в golden НЕ входят: фильтры не меняют
байты выживших постов; их семантика (membership / regex) закрепляется парой
unit-тестов — дешевле и меньше площадь флаки-рисков.

**None-date пост в этап 0 НЕ входит** (в записанных данных его и не бывает):
текущий код падает на нём в ДЕФОЛТНОМ пути (naive/aware TypeError в
sort-ключе групп, см. §1). Синтетическая None-date фикстура добавляется в
HTML-golden коммитом этапа 4 со ссылкой на §3.11–12.

Снимаются и кладутся в `tests/test_data/golden/`:

- RSS XML — полный выход `generate_channel_rss` (tg_cache замокан);
- HTML-фид — полный выход `generate_channel_html`.

**Детерминизм снапшотов (обязательные меры):**

- TZ раннера пинится: `os.environ['TZ'] = 'UTC'; time.tzset()` в conftest
  (naive-даты фикстур интерпретируются `.timestamp()`-ом в локальной TZ —
  без пина pubDate плавает между машинами);
- волатильные строки RSS XML нормализуются перед сравнением:
  `<lastBuildDate>` (feedgen 1.0.0 ставит now() ОДИН раз в конструкторе
  `FeedGenerator` — байты стабильны внутри процесса, но меняются между
  прогонами) вырезается regex'ом и в golden, и в актуальном выводе;
  `<generator>` в feedgen 1.0.0 версии НЕ содержит — его нормализация не
  обязательна, оставлена как дешёвая страховка от апгрейда библиотеки;
- корпус не содержит постов, где `pubDate` берётся из `datetime.now()`
  (это только None-date случай — он исключён, см. выше);
- порядок merged-флагов ДО этапа 2 недетерминирован: `list(set(...))`
  (rss_generator.py:260–265) зависит от PYTHONHASHSEED процесса, который из
  conftest не запинить — содержимое `<div class="message-flags">`
  нормализуется сортировкой при сравнении golden; нормализация снимается
  коммитом этапа 2 со ссылкой на §3.8;
- ключ подписи media-URL пинится autouse-фикстурой
  `monkeypatch.setattr(KeyManager, "signing_key", ...)` (образец —
  tests/test_new_media_types.py): иначе golden, снятый на dev-машине,
  краснеет на CI — свежий checkout генерирует новый `secrets.token_hex`
  в `data/media_digest.key`, и все digest'ы в URL меняются;
- корпус подаётся monkeypatch'ем `tg_cache.cached_get_chat` /
  `cached_get_chat_history` (возврат распикленных записанных объектов) —
  lazy-import внутри фид-функций разрешает имена поздно, поэтому патч модуля
  tg_cache работает (образец — test_stage4_eventloop.py);
- запись media-id пинится: monkeypatch `upsert_media_file_ids_bulk_sync`
  (или `DB_PATH` → tmp_path) — иначе golden-тесты с медиа-фикстурами пишут
  в реальную `./data/media_file_ids.db` (`DB_PATH` cwd-относителен,
  file_io.py:13); байты фида это не меняет (flush глотает ошибки), но
  side-effect вне tests/ недопустим; образец — test_stage4_eventloop.py:164;
- фикстура time-based кластера требует `time_based_merge=True`: ключ читает
  только `rss_generator.Config` — его патча достаточно; `post_parser.Config` —
  независимый dict из того же `get_settings()`, патчить оба — дешёвая
  страховка от будущего дрейфа, но не необходимость.

Следствие пина TZ=UTC: дельту §3.7 (сдвиг TZ даты merged-футера) golden
физически не видит — она верифицируется выделенным тестом этапа 2 с не-UTC TZ.

Оракул эквивалентности всех этапов. Обновление golden — только коммитом со
ссылкой на пункт §3. Ожидаемые изменения: этап 1 (пп.1, 3, 4; пп.2 и 5 —
fail-closed ветки, golden их НЕ видит — верифицируются юнит-тестами
sanitizer), этап 2 (пп.6, 8, 15 + снятие нормализации флагов), этап 4
(добавление None-date фикстуры в HTML-golden, пп.11–12), этап 5b (п.14 — фикстура
webpage-без-фото меняет и feed-level байты: до 5b незакрытый div
дозакрывался bleach'ем в конце фрагмента).

DoD: записанный корпус + отчёт инвентаризации + снапшоты в репо; тест
сравнения зелёный и детерминированный (два прогона подряд — идентичные
байты); любое изменение рендера валит его с внятным диффом. Пиклы корпуса
сцеплены с версией kurigram — при апгрейде библиотеки корпус перезаписывается
с сервера (зафиксировать в README тестов).

### Этап 1 — `sanitizer.py` + санитайз в пайплайне (ишью #22, обновить)

```python
# sanitizer.py — the ONLY bleach configuration in the project.
ALLOWED_TAGS = ['p', 'a', 'b', 'i', 'strong', 'em', 's', 'del',
                'ul', 'ol', 'li', 'br', 'div', 'span',
                'img', 'video', 'audio', 'source']          # union of the 3 old copies
ALLOWED_ATTRIBUTES = { ... }        # identical in all 3 copies — move as-is
ALLOWED_CSS_PROPERTIES = ["max-width", "max-height", "object-fit", "width", "height"]
ALLOWED_PROTOCOLS = ['http', 'https', 'tg']   # non-default! tg:// links in footers

# Shared instance is safe: CSSSanitizer only holds the allowed-properties list
# (stateless config); bleach builds a fresh Cleaner per clean() call anyway.
_CSS_SANITIZER = CSSSanitizer(allowed_css_properties=ALLOWED_CSS_PROPERTIES)

def sanitize_html(html_raw: str, log_context: str = "") -> str:
    """Sanitize one HTML fragment. FAIL-CLOSED: on any bleach error the
    fragment is html.escape()d, never returned raw (stored-XSS guard).
    log_context (e.g. "channel X, message_id Y") is included in error/slow
    logs to keep operational grep-ability."""
    # The FULL call — both non-default params are load-bearing:
    #   clean(html_raw, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRIBUTES,
    #         protocols=ALLOWED_PROTOCOLS, css_sanitizer=_CSS_SANITIZER,
    #         strip=True)
    # strip=True: disallowed tags are REMOVED (bleach default False would
    # escape them into visible text). strip_comments stays default (True),
    # matching all current call sites. Keep the >0.05s diag_sanitize_slow
    # warning with input_len here.
    # Error log name: html_sanitization_error (SINGLE name replacing the
    # three per-path names — registry §3.16); log_context distinguishes
    # the call sites, tests assert the name.
```

Правки:

1. `post_parser._sanitize_html` (branch:702–737) → делегат в
   `sanitizer.sanitize_html`. Fail-open ветка исчезает (§3.2).
2. `_render_pipeline` получает параметр `channel` (только для логов) и после
   `_render_messages_groups` (т.е. после фильтров) выполняет
   `p['html'] = sanitize_html(p['html'], log_context=f"channel {channel}, message_id {p['message_id']}")`.
   Комментарий: the pipeline already runs in a worker thread.
   ВАЖНО: в этапах 1–2 близнецы ещё не слиты — аргумент `channel` добавляется
   в ОБА вызова `to_thread(_render_pipeline, …)` (rss_generator.py:433 и 657).
3. RSS-цикл (branch:458–497): удалить вложенный `_sanitize_sync` + `to_thread`;
   `fe.content(content=post['html'], type='CDATA')` напрямую.
4. HTML-путь (branch:671–705): удалить `_concat_html`/`_sanitize_sync` +
   оба `to_thread`; `html = '\n<hr class="post-divider">\n'.join(...)` —
   join ПОСЛЕ санитайза (§3.3, §3.4).
5. Импорты bleach/CSSSanitizer из rss_generator удалить. Тест
   test_stage4_eventloop.py:474 monkeypatch'ит `rss_module.HTMLSanitizer` —
   перенацелить на `sanitizer` (API relocation).
6. Обновить golden (§3 пп.1–5) и комментарии о границах санитайза.

Тесты: `tests/test_sanitizer.py` — s/del выживают; script/onerror ВЫРЕЗАЮТСЯ
(не эскейпятся в текст — проверка strip=True); `tg://` href выживает;
fail-closed при исключении; hr-разделитель в HTML-фиде.

DoD: одно определение allowed_tags в репо; в rss_generator нет bleach;
pytest + golden зелёные.

### Этап 2 — выпил `processed_message_to_tg_message`

```python
# Select the main RAW message with the same criterion the processed dicts used:
# first message that has text or caption, else the first of the group.
main_idx = next((i for i, m in enumerate(group) if (m.text or m.caption)), 0)
main_raw = group[main_idx]
main_message = processed_messages[main_idx]

# Deterministic merged flags: first-seen order, then 'merged'.
merged_flags = list(dict.fromkeys(f for msg in processed_messages for f in msg['flags']))
merged_flags.append("merged")

footer_html = post_parser.generate_html_footer(main_raw, flags_list=merged_flags)
```

Дополнительно (§3.15): в `_reactions_views_links` пустая `reactions_html` НЕ
добавляется в `first_line_parts` (иначе после перехода на реальный Message
merged-посты с пустым reactions-объектом получили бы ведущий разделитель —
артефакт, который сегодня есть и у одиночных постов; чиним для всех).

Удалить конвертер целиком и импорт `SimpleNamespace`. Обновить golden
(§3 пп.6, 8, 15) и СНЯТЬ сорт-нормализацию `message-flags` в
golden-сравнении, введённую этапом 0: порядок флагов теперь детерминирован
(§3.8), нормализация больше не нужна и лишь маскировала бы регрессии.

Тесты: футер merged-группы — реальные ссылки главного сообщения; несколько
custom-emoji → отдельные «❓ N» span'ы; paid → «⭐ N»; пустой reactions-объект →
нет ведущего разделителя (и для одиночного поста); порядок флагов стабилен;
**выделенный тест §3.7 с не-UTC TZ** (например `TZ=Europe/Moscow` + `tzset`):
дата merged-футера совпадает с датой футера одиночного поста того же
сообщения. TZ ОБЯЗАТЕЛЬНО восстанавливается в teardown (fixture/try-finally),
иначе не-UTC протечёт в golden-тесты, которым conftest запинил UTC.

DoD: функция удалена, pytest + golden зелёные.

### Этап 3 — слияние близнецов вокруг `_prepare_feed_posts`

```python
@dataclass
class PreparedFeed:
    channel_username: str
    channel_title: str            # used by RSS metadata only
    posts: list[dict]             # rendered, filtered, sorted, SANITIZED

class ChannelNotFound(Exception):
    def __init__(self, channel_identifier):   # prepared id, for create_error_feed
        self.channel_identifier = channel_identifier

# NO timed() abstraction: it could not carry the paired rss_/html_ log-line
# names nor the extra context (message counts) anyway. Keep today's explicit
# logger.debug lines, prefixed via log_prefix.

async def _prepare_feed_posts(channel, client, *,
                              limit, exclude_flags, exclude_text, merge_seconds,
                              history_limit, enrich_replies: bool,
                              log_prefix: str) -> PreparedFeed:
    # log_prefix: 'rss' | 'html' — preserves today's paired log-line names
    # (f"{log_prefix}_channel_info_timing", f"{log_prefix}_messages_retrieval_timing", ...)
    # 1) validate limit (1..200)
    # 2) channel_name_prepare + cached_get_chat:
    #    UsernameInvalid/UsernameNotOccupied/no-username -> ChannelNotFound(prepared)
    #    FloodWait -> re-raise (api_server maps to 429)
    #    other errors -> ValueError chain (unified text, §3.10)
    # 3) cached_get_chat_history(limit=history_limit):
    #    FloodWait -> re-raise BEFORE the ValueError wrap (§3.9)
    # NOTE: keep `from tg_cache import ...` INSIDE this function — feed tests
    # monkeypatch tg_cache and rely on late name resolution.
    # 4) if enrich_replies: messages = await _reply_enrichment(client, messages)
    # 5) try: to_thread(_render_pipeline, ...) finally: flush_pending_media_ids()
```

Форматтеры:

- `generate_channel_rss`: `history_limit=limit*2, enrich_replies=False,
  log_prefix="rss"` (RSS over-fetches so merging still yields ~limit posts) →
  fg-метаданные + entries (`rss_date_range`-лог сохраняется) →
  `to_thread(fg.rss_str)`.
- `generate_channel_html`: `history_limit=limit, enrich_replies=True,
  log_prefix="html"` (enrichment is HTML-only to keep RSS polling cheap —
  deliberate) → join с `<hr>`.
- Оба: `except ChannelNotFound as e: return create_error_feed(str(e.channel_identifier), base_url)`
  (байт-эквивалентно текущему выводу: prepared-значение уже используется
  сегодня, int/str интерполируются одинаково — проверено в ревью).
- Внешние catch-логи ОСТАЮТСЯ в форматтерах со своими сегодняшними именами
  (`generate_channel_rss: …` — rss_generator.py:526, `html_generation_error:
  …` — 716): каждый форматтер сохраняет собственный внешний try/except.

Тесты: golden без изменений (главный критерий); ChannelNotFound → error feed
в обоих путях; FloodWait из get_chat → 429; FloodWait из истории → 429
(новый, §3.9); ValueError из истории → 400.

DoD: один блок get_chat/get_history/render; diffstat отрицательный;
pytest + golden зелёные.

### Этап 4 — группировка без deepcopy и мутаций

```python
def _compute_time_based_group_ids(messages, merge_seconds) -> dict[int, str | int]:
    """Return {message.id: effective_media_group_id} WITHOUT mutating messages.

    Contract: all messages belong to ONE chat (message.id is unique only
    per chat); callers must not mix chats in a single call.

    Reproduces the old algorithm for every input the old code survived on
    PRODUCTION data (naive kurigram dates). Inputs only aware-date test mocks
    could produce (fully-None and aware+None mixes, where the old code
    clustered the None-date tail by insertion order, incl. truthy-id
    adoption) are deliberately replaced — registry §3.11:
    - messages WITHOUT a date do not participate in time clustering and get
      NO mapping entry (their own media_group_id still applies downstream) —
      registry §3.11;
    - sort by date ascending, timestamp-based key (naive-safe);
    - a message joins the current cluster if the gap to the PREVIOUS message
      is <= merge_seconds; the gap is computed by NAIVE datetime subtraction
      (msg.date - prev.date).total_seconds(), exactly as the old code — NOT
      via timestamps (they diverge during a DST fold, and the old behavior
      is the contract);
    - effective id of a cluster = the FIRST TRUTHY media_group_id seen in
      cluster order (old code used truthiness, not `is not None`; it also
      overwrote members' own differing ids — keep that);
    - if no member has a truthy id and len(cluster) >= 2:
      synthetic id f"time_{min(dates)}" (keep the exact format);
    - singleton clusters and clusters with no effective id produce NO
      entries. Every member of a cluster with an effective id gets an entry.
    """
```

Правки:

- `_create_messages_groups(messages, group_ids=None)`:
  `effective = (group_ids or {}).get(message.id, message.media_group_id)`;
  sort-ключ групп — timestamp-based (naive-безопасный), фолбэк для None-date
  групп `float('inf')` → детерминированно переживают `[:limit]`-срез как
  новейшие; финальная сортировка постов в `_render_messages_groups`
  (фолбэк `0.0`) НЕ меняется — None-date посты выводятся в конце фида (§3.12).
- `_render_pipeline` при `time_based_merge`: маппинг + пред-сортировка входа
  date-ASC тем же timestamp-ключом (фолбэк `+inf` — None-date в конце);
  стабильный sorted на fetch-order входе воспроизводит порядок старого
  `messages_sorted`, включая ties (старый sorted работал на deepcopy того же
  fetch-order списка, мутации происходили после сортировки — проверено).
  `sorted()` не мутирует вход — deepcopy не нужен.
- Удалить `_create_time_based_media_groups` и импорт `copy`; поправить
  модульный импорт в test_stage4_eventloop.py:34–42 (API relocation).
- Добавить None-date фикстуру ТОЛЬКО в HTML-golden (§3.11–12): в RSS
  None-date пост получает `pubDate = datetime.now()` (rss_generator.py:
  503–507) — недетерминированно, и новую нормализацию для этого не вводим;
  RSS-семантика None-date закрепляется unit-тестом (pubDate присутствует,
  entry сгенерирован), вне golden.

Тесты (`tests/test_group_ids.py`, naive-даты): time-кластер без id →
синтетический; усыновление truthy id с backfill и перезаписью чужого id;
falsy id (`0`, `""`) игнорируется как в старом коде; одиночка без entry;
None-date не получает entry в маппинге, но его собственный `media_group_id`
продолжает действовать (медиагруппа с None-датами собирается); смешанный
None-date вход не падает (§3.11); полностью-None вход: новое поведение
закреплено явно; None-date группы переживают `[:limit]`-срез (ключ групп
`+inf`), в итоговом выводе — в конце фида (финальный фолбэк `0.0`, §3.12);
вход не мутирован;
ties при равных датах → порядок как у старого кода; кластеризация через
DST-fold — как у старого кода (naive-вычитание).

DoD: `copy.deepcopy` отсутствует; pytest + golden зелёные (golden-дифф — только
добавленная None-date фикстура, §3.11–12).

### Этап 5 — таблица медиа-типов в post_parser (два коммита)

**5a — рефакторинг байт-в-байт.**

Шаг 0: fragment-level снапшоты `_generate_html_media` (ДО санитайза) для всех
типов: PHOTO, VIDEO, ANIMATION, VIDEO_NOTE, AUDIO, VOICE, STICKER img/video,
DOCUMENT pdf/обычный, LIVE_PHOTO, STORY video/photo, POLL с description_media,
PAID_MEDIA, WEB_PAGE с фото (пустой media-div!), **и edge-ветки**: WEB_PAGE
без фото (незакрытый div — воспроизводится в 5a буквально), file_unique_id
is None, channel_username is None, гейт webpage-превью «text ≤ 10».

Источник сообщений для fragment-снапшотов (v6): ЗАПИСАННЫЙ КОРПУС этапа 0 —
для всех типов, которые в нём есть (по инвентарю: PHOTO, VIDEO, ANIMATION,
VIDEO_NOTE, AUDIO, VOICE, STICKER, DOCUMENT, POLL, WEB_PAGE, GIVEAWAY,
GIVEAWAY_WINNERS). Моки — ТОЛЬКО для отсутствующих в корпусе типов
(PAID_MEDIA, STORY, LIVE_PHOTO, CHECKLIST и прочая экзотика) — ограничение
инвариант-теста «ложная зелень на моках» сужается до этих типов.

```python
# Single source of truth: media type -> (object selector, render kind).
# kind=None means "selector-only entry": the object participates in file-id
# extraction/collection, but rendering happens outside the dispatcher.
# The ONLY kind=None entry is WEB_PAGE (rendered by _format_webpage).
# PAID_MEDIA has NO entry at all: its info block stays a separate branch in
# _generate_html_media, and it is deliberately not collected/downloadable.
MEDIA_SOURCES: dict[MessageMediaType, Callable[[Message], tuple[Any, str | None]]] = {
    MessageMediaType.PHOTO:    lambda m: (m.photo, 'img_400'),
    MessageMediaType.DOCUMENT: _select_document,   # returns kind 'pdf' OR 'img_400' by mime_type
    MessageMediaType.STICKER:  _select_sticker,    # video_loop_200 vs img_200_sticker
    MessageMediaType.STORY:    _select_story,      # maps helper kinds video->'video_400', img->'img_400'
    MessageMediaType.POLL:     _select_poll_media, # same helper-kind mapping
    MessageMediaType.WEB_PAGE: lambda m: (getattr(m.web_page, 'photo', None), None),
    # VIDEO/ANIMATION/VIDEO_NOTE -> video_400; AUDIO/VOICE -> audio;
    # LIVE_PHOTO -> video_loop_400
}

@dataclass
class RenderCtx:
    url: str                      # signed /media URL — assembled by _generate_html_media,
                                  # which KEEPS the digest call and the channel_username
                                  # guard inline (they are not the renderer's business)
    tg_link: str | None = None    # t.me deep link — only the 'pdf' renderer uses it
    emoji: str = ''               # sticker alt text
    mime: str | None = None       # audio/voice source type; the DEFAULT is chosen
                                  # by the ctx builder in _generate_html_media BY
                                  # MEDIA TYPE (AUDIO -> audio/mpeg, VOICE ->
                                  # audio/ogg, branch:907/912) — the renderer
                                  # receives a ready value and never guesses

# Renderers, NOT naked format strings: a renderer returns list[str] so the
# byte structure of '\n'.join is preserved (audio emits TWO items: tag + <br>;
# pdf emits its two-append div block). Renderer bodies are lifted VERBATIM
# from the existing if/elif branches — including the `src="{url}"style=`
# concatenation artifacts — byte-for-byte fidelity is the 5a contract.
RENDERERS: dict[str, Callable[[RenderCtx], list[str]]] = { ... }
```

PDF идёт ЧЕРЕЗ таблицу: `_select_document` возвращает kind `'pdf'` для
`application/pdf`, `RENDERERS['pdf']` использует `ctx.tg_link` (сборка ссылки
`t.me/c/...` vs `t.me/...` — в `_generate_html_media`, как сейчас).

Потребители: `_get_file_unique_id`, `_save_media_file_ids`,
`_generate_html_media` — все через селектор. Ограничение: `flags.append(...)`
НЕ выносить из `_extract_flags` (эндпоинт `/flags` парсит его исходник через
`inspect.getsource`); тест: `get_all_possible_flags()` непуст и содержит
известные флаги.

Межмодульный инвариант-тест (границу с api_server закрепить машинно):
для каждого типа из MEDIA_SOURCES объект, выбранный селектором, находится
`api_server.find_file_id_in_message` по своему `file_unique_id` — новые entry
таблицы не могут дать URL, который download-путь не разрешит (иначе /media
404). Ограничение теста: обе стороны работают на моках — класс багов «обе
функции ждут атрибут, которого нет у реального kurigram-объекта» он не ловит;
опциональная best-effort митигация — сверка атрибутов моков с
`pyrogram.types` (сама интроспекция хрупка при апгрейдах kurigram; ядро
теста ценно и без неё, обязательной её не делать).
Сама `find_file_id_in_message` НЕ сливается с таблицей: её контракт шире —
поиск любого скачиваемого объекта независимо от `message.media`, включая
`explanation_media`, который рендер сознательно игнорирует (§7).

**5b — зарегистрированные фиксы** (отдельный коммит): закрыть
`</div>` во всех ветках (§3.14); guard «>100 МБ» на любой медиа-объект
(§3.13); обновить fragment-снапшоты с diff-комментарием. Feed-level golden
5b не трогает сверх этих пунктов.

DoD: прямые атрибутные ЦЕПОЧКИ вида `m.photo.file_unique_id` — только в
селекторах таблицы, хелперах `_poll_media_object`/`_story_media_object`
(это и есть реализация селекторов) и `_format_webpage`. Потребители
извлекают uid единообразно — `getattr(selected_obj, 'file_unique_id', None)`
от объекта, ВОЗВРАЩЁННОГО селектором; санкционированные точки извлечения:
`_get_file_unique_id`, `_save_media_file_ids`, инвариант-тест. Три лестницы
удалены; снапшоты обоих уровней + pytest зелёные; инвариант-тест с
api_server зелёный.

### Этап 6 — косметика

1. `_wrap_post_html(body, footer)` — единая обёртка (сейчас три копии:
   `_format_html` и обе ветки `_render_messages_groups`).
2. Инлайн-стили 400px/200px → константы (если не закрыто этапом 5).
3. `_trim_messages_groups` → инлайн-срез `[:limit]`; поправить импорт в
   test_stage4_eventloop.py (API relocation).
4. Комментарии «stage-4»/«4.4» — переписать под новую границу санитайза.
5. Фильтр exclude_flags → comprehension; фильтр exclude_text — оставить
   циклом или обернуть предикатом, но `excluded_post`-debug-лог СОХРАНИТЬ
   (единственный след, почему пост выпал из фида).

## 6. Порядок и зависимости

`0 → 1 → 2 → 3` строго последовательно. `4` и `5` независимы, после 3.
`6` — последним. Каждый этап мержибелен сам по себе.

## 7. Сознательно НЕ делаем (отложено)

- Унификация глубины истории RSS (`limit*2`) vs HTML (`limit`).
  Симптом: кэш истории — ОДИН файл на канал, limit хранится внутри и при
  несовпадении — miss (tg_cache.py:43–52, 106–109); канал с обоими
  потребителями живёт в вечном miss-цикле со взаимным вытеснением и
  удвоенным RPC. НЕ отложено — чинится в отдельном эпике кешей (ишью #23,
  Package B: префиксная выдача истории, `cached_limit >= limit` → hit).
  Здесь ничего делать не нужно, но при мёрже учитывать пересечение (см.
  комментарий к эпику #34).
- `_reply_enrichment` в RSS-пути (RPC-нагрузка от частых опросов ридеров).
- HTML-страница ошибки для `generate_channel_html` (сейчас RSS-XML).
- Кэширование/демутация `_reply_enrichment` (мутирует кэшированные Message).
- `api_server.find_file_id_in_message`: остаётся отдельной (контракт шире
  рендера — см. этап 5a), граница закрыта инвариант-тестом.
- Валидация `merge_seconds` в api_server (сейчас принимает `<=0` из query).
- Валидация `exclude_text` (невалидный regex → `re.error` → 500 и сейчас,
  и после этапа 3 — api_server пробрасывает параметр как есть).
- CDATA-риск feedgen: html5lib не эскейпит `>` в атрибутах, `]]>` внутри href
  теоретически рвёт CDATA. Пре-существующий, вне скоупа.
- `/flags` через `inspect.getsource` — хрупкая механика, замена отложена.

## 8. Журнал адверсариального ревью

### Раунд 1 (по v1): 26 претензий (2 blocker, 8 major, 12 minor, 4 nit)

- Приняты полностью и внесены: №3 (protocols), №4 (правки тестов при API
  relocation), №5 (golden-оракул → этап 0), №6 (TZ merged-футера, §3.7),
  №7 (FloodWait истории → 429, §3.9), №11 (§3.10), №12 (lazy-import пин),
  №13 (наблюдаемость, log_context), №14 (реальная дельта реакций, §3.6),
  №16+№15 (naive/aware краш → §3.11–12, truthiness), №18 (edge-матрица
  снапшотов), №19 (payload ChannelNotFound), №20 (§3.5), №21 (обоснование
  шареного CSSSanitizer), №22 (/flags-мина), №23 (excluded_post-лог),
  №24 (контракт one-chat), №25 (§2), №26 (CDATA в §7).
- №1 (blocker): снято разделением этапа 5 на 5a/5b и двумя слоями эталонов.
- №2 (blocker): понижено до пункта реестра §3.4 по согласованной формулировке.
- №8: контракты `find_file_id_in_message` и MEDIA_SOURCES признаны разными;
  граница закрыта инвариант-тестом по предложению критика.
- №9: закрыт пред-сортировкой входа naive-безопасным ключом.
- №10: база запинена на f9550d8.
- №17: `TAG_TEMPLATES: dict[str, str]` заменён на renderer-функции.

### Раунд 2 (по v2): 13 претензий (1 blocker, 4 major, 5 minor, 3 nit)

- №1 (blocker): краш naive/aware живёт в ДЕФОЛТНОМ пути сортировки групп, а
  не только в тайм-кластеризации (подтверждено эмпирически) → §1 исправлен,
  None-date фикстура перенесена из этапа 0 в этап 4 (вариант «а» критика),
  §3.12 уточнён.
- №2: направление дельты пустых реакций было инвертировано → выбран
  fix-вариант: §3.15 (чинится для всех постов), формулировка §3.6 очищена.
- №3: `strip=True` внесён в спеку полным вызовом `clean()` (тот же класс
  дыры, что protocols в раунде 1).
- №4: детерминизм golden обеспечен (TZ-пин, нормализация lastBuildDate/
  generator); зафиксировано, что §3.7 golden не верифицирует → выделенный
  не-UTC тест в этапе 2.
- №5: противоречие DOCUMENT/PDF разрешено в пользу «PDF через таблицу»,
  ctx расширен полем tg_link.
- №6: RenderCtx специфицирован (поля, владелец сборки URL, verbatim-правило).
- №7: базис gap зафиксирован — naive-вычитание как в старом коде (timestamp
  расходится в DST-fold).
- №8: позиция None-date групп зарегистрирована (§3.12: новейшие, `+inf`).
- №9: формулировка теста исправлена («нет entry в маппинге», а не «синглтон»).
- №10: ограничение инвариант-теста (ложная зелень на моках) проговорено +
  митигация.
- №11: `_render_pipeline` получает `channel` для log_context (этап 1 п.2).
- №12: диаграмма §2 исправлена (фильтры/sort внутри `_render_messages_groups`,
  санитайз после фильтров).
- №13: DoD 5a включает хелперы `_poll_media_object`/`_story_media_object`.
- Чистыми признаны: эквивалентность error feed (B5), §3.9 против tg_cache и
  429/Retry-After (B6), эквивалентность пред-сортировки (B3), внесение всех
  исходов раунда 1.

### Раунд 3 (верификация дельты v3)

- Пять из шести выборов подтверждены (None-date → этап 4; fix пустых реакций;
  PDF через таблицу; naive-gap; полный clean()).
- Возражение по №5 принято: `+inf` определяет только выживание группы при
  `[:limit]`-срезе, итоговую позицию задаёт финальная сортировка с фолбэком
  `0.0` (None-date посты — в конце фида, как и раньше) — §3.12, этап 4 и его
  тест переформулированы.
- Криво внесённое исправлено: список ожидаемых golden-изменений дополнен
  этапом 5b (п.14); исключения single-post пути в §2 расширены до
  пп. 2, 13, 14, 15.
- Практические заметки внесены: teardown TZ в не-UTC тесте §3.7; два
  независимых Config-дикта при monkeypatch `time_based_merge`.

### Раунд 4 (по v3): 17 пунктов (2 major, 4 minor, остальное nit/экономика)

- Детерминизм golden добит двумя major-находками: порядок merged-флагов до
  этапа 2 зависит от PYTHONHASHSEED (`list(set)`) → сорт-нормализация
  `message-flags` до этапа 2; ключ подписи media-URL (`secrets.token_hex` в
  `data/media_digest.key`) → пин autouse-фикстурой.
- Согласован состав фикстур: добавлен strikethrough-пост (иначе §3.1
  невидим), пп.2/5 помечены как невидимые для golden; exclude-сценарии
  вынесены из golden в unit-тесты (экономическая критика).
- `timed()` выкинут: не мог сохранить парные `rss_/html_` имена логов и не
  окупался — заменён параметром `log_prefix` у `_prepare_feed_posts`.
- Кодер-готовность: оба call-site'а `_render_pipeline` в этапах 1–2
  проговорены; владелец mime-дефолта — ctx-билдер по media-типу; указатель
  на паттерн monkeypatch tg_cache в этапе 0; обоснование двойного
  Config-патча исправлено (достаточно rss_generator.Config).
- Митигация инвариант-теста через интроспекцию `pyrogram.types` понижена до
  опциональной (сама хрупка при апгрейдах).
- §7 пополнен: вечный miss-цикл кэша истории у dual-consumer каналов (цена
  отложенной унификации глубины), валидация exclude_text.
- C-остатки признаны чистыми: частичный flush и дубли upsert закреплены
  существующими тестами; лог одиночного санитайза контекста и сегодня не
  имеет; api_server пробрасывает exclude_* без валидации — поведение не
  меняется.
- Экономический вердикт критика: аппарат тяжёл для ~700 строк рефакторинга,
  но каждый элемент отвечает конкретному найденному багу; срезаны только
  `timed()`, exclude-фикстуры и обязательность интроспекции. Этапы не
  сливать: раздельная мержибельность дешевле.

### Раунд 5 (по v4): независимый холодный проход, 8 пунктов (1 blocker, 1 major)

Второй критик — без контекста предыдущих раундов, с запретом доверять
журналу и реестру на слово. Итог валидации: ВСЕ line-refs §1 и ключевые
эмпирические утверждения спеки подтверждены независимо (включая вырезание
hr, заглатывание постов незакрытым div, strip/protocols-семантику, TypeError
в дефолтном пути, miss-цикл кэша, соответствие MEDIA_SOURCES реальной
лестнице вплоть до артефактов, находимость всех селекторных объектов в
find_file_id_in_message); архитектурные решения признаны чистыми. Найдены
дефекты исполнимости:

- Blocker: None-date фикстура в RSS-golden недетерминированна
  (`pubDate = now()` для None-date entry) → фикстура включается только в
  HTML-golden, RSS-семантика — unit-тестом (этап 4 переписан).
- Major: три имени санитайз-логов физически сливаются в одно при делегате —
  противоречие с §4 → зарегистрировано как §3.16 (единое
  `html_sanitization_error` + log_context); внешние catch-логи форматтеров
  явно оставлены в этапе 3.
- Механизм волатильности feedgen уточнён (lastBuildDate — один раз в
  конструкторе, не при сериализации; generator без версии — нормализация
  опциональна).
- §3.11/контракт этапа 4: «Было» дополнено выжившими aware+None и
  fully-None входами (кластеризация None-хвоста с adoption), заголовок
  docstring сужен до «survived on production data».
- DoD 5a переписан: единообразное извлечение uid через
  `getattr(selected_obj, ...)` с санкционированными точками (сигнатура
  селектора uid не возвращает — старая формулировка была невыполнима).
- Этап 0: добавлен пин записи media-id (cwd-относительный DB_PATH писал бы
  в реальную data/ из golden-тестов); поправлен line-ref api_server;
  каветт §3.12 про будущие даты.

Отклонённых претензий нет; по всем спорным пунктам достигнут консенсус.

### v6 — golden-корпус: записанные реальные сообщения (решение владельца)

Не раунд ревью — изменение дизайна по решению владельца проекта, с
немедленной эмпирической проверкой:

- Корпус этапа 0 переведён с синтетики на записанные кэши прод-сервера
  (`data/tgcache/*.cache|.chatinfo` — пиклы того же формата, что читает
  прод-путь cache-hit). Мотив: холодный проход (раунд 5) пометил класс
  «мок и код согласованно ждут несуществующий атрибут» как неловимый на
  моках — реальные объекты закрывают его целиком.
- Снапшот кэша снят (90 каналов), совместимость пиклов со старым kurigram
  проверена под 2.2.23: 179/179 файлов без ошибок.
- Инвентарь покрытия: 8582 сообщения, все даты naive (посылка спеки
  подтверждена данными); покрыты в т.ч. edge-кейсы §3.1 (зачёркивания: 150)
  и §3.15 (пустые reactions-объекты: 17), webpage с/без фото, ~294
  time-кластерные пары. Синтетика сузилась до None-date и отсутствующих
  типов (PAID_MEDIA, STORY, LIVE_PHOTO, CHECKLIST, прочая экзотика).
- Fragment-снапшоты этапа 5a — тоже на корпусе, где тип доступен;
  ограничение инвариант-теста сузилось до mock-only типов.
