# Pyrogram Bridge

[English](README.md) · [Русский](README.ru.md)

Turns Telegram channels into RSS feeds. The bridge logs in to Telegram as a regular user
account (MTProto, via [Kurigram](https://github.com/KurimuzonAkuma/pyrogram), a Pyrogram
fork), reads channel history and serves it over HTTP as RSS, as an HTML page or as JSON —
with media, albums, replies and reactions, and with filters that drop the posts you do not
want to read.

It reads whatever that account can read: public channels by username, and private channels
the account is subscribed to by numeric id. Nothing supernatural — it is just one more
Telegram client.

It pairs with [miniflux-tg-add-bot](https://github.com/vvzvlad/miniflux-tg-add-bot): forward a
post to the bot and it subscribes the channel's bridge feed in Miniflux, then lets you switch
the feed's filters with buttons.

---

## Features

- **An RSS feed per channel** — `/rss/<channel>`; the same posts can also be rendered as one
  HTML page.
- **Single posts** as HTML or JSON — `/html/<channel>/<id>`, `/json/<channel>/<id>`.
- **Media through the bridge** — photos, videos and files are downloaded from Telegram,
  cached in the data volume and served from signed URLs, so the RSS reader never needs
  Telegram access.
- **Albums become one entry**; optionally, posts published a few seconds apart are merged too.
- **Post filtering** — drop posts by built-in flags (ads, forwards, links to other channels,
  streams, clown-reaction memes, …) and by your own regex, per feed, right in the feed URL.
  See [Filtering posts](#filtering-posts).
- **Full rendering** — replies with the quoted post, polls, link previews, Telegram Rich
  Messages, reactions, views and links back to Telegram.
- **Gentle on Telegram** — channel info and history are cached and live calls are throttled;
  a FloodWait turns into HTTP 429 with `Retry-After`; feeds answer `ETag`/`Last-Modified`
  conditional requests with 304.
- **Self-healing** — an in-process watchdog restarts a stuck Telegram session, and `/ping`
  serves the container healthcheck.

## Quick start (Docker)

1. Get an `api_id` and `api_hash`: log in at <https://my.telegram.org/apps> →
   *API development tools* → create an application.

2. Create a `docker-compose.yml` (or a stack in Portainer):

   ```yaml
   volumes:
     pyrogram_bridge:

   services:
     pyrogram_bridge:
       image: gitea.vvzvlad.xyz/projects/pyrogram-bridge:latest
       container_name: pyrogram-bridge
       environment:
         TG_API_ID: 12345678
         TG_API_HASH: 0123456789abcdef0123456789abcdef
         PYROGRAM_BRIDGE_URL: https://pgbridge.example.com
         API_PORT: 80
         TOKEN: change-me
         TZ: Europe/Moscow
       restart: always
       volumes:
         - pyrogram_bridge:/app/data
       labels:
         traefik.enable: "true"
         traefik.http.routers.pgbridge.rule: Host(`pgbridge.example.com`)
         traefik.http.services.pgbridge.loadBalancer.server.port: 80
         traefik.http.routers.pgbridge.entrypoints: websecure
         traefik.http.routers.pgbridge.tls: true
   ```

   The repository's [`docker-compose.yml`](docker-compose.yml) is a fuller example: every
   optional variable with a comment, log rotation and a healthcheck.

3. Log in to Telegram once, interactively, **before** starting the bridge for real. Without a
   session the bridge asks for a phone number on start; a container running in the background
   has no terminal to ask in, so it fails at start and keeps restarting.

   With Docker Compose, from the directory with the compose file:

   ```bash
   docker compose run --rm pyrogram_bridge
   ```

   With Portainer or plain Docker: deploy the stack, stop it, and run a one-off container on
   the stack's volume. Compose and Portainer prefix the volume name with the project/stack
   name, so look it up with `docker volume ls`:

   ```bash
   docker run --rm -it -v <stack>_pyrogram_bridge:/app/data \
     -e TG_API_ID=12345678 -e TG_API_HASH=0123456789abcdef0123456789abcdef \
     gitea.vvzvlad.xyz/projects/pyrogram-bridge:latest
   ```

   Enter the phone number, the code Telegram sends you and, if two-step verification is on,
   the password:

   ```text
   Enter phone number or bot token: +7 900 000 00 00
   Is "+7 900 000 00 00" correct? (y/N): y
   The confirmation code has been sent via Telegram app
   Enter confirmation code: 12345
   The two-step verification is enabled and a password is required
   Password hint: None
   Enter password (empty to recover): ********
   ```

   Wait for `INFO:     Application startup complete.` and stop it with Ctrl+C.

4. Start the bridge: `docker compose up -d` (or start the stack).

The session is saved as `pyro_bridge.session` in the data volume (`/app/data` inside the
container). It *is* the logged-in account: keep it, and do not share it.

Check that it works: `curl https://pgbridge.example.com/rss/DragorWW_space/change-me`.

## Usage

### Endpoints

| URL | What you get |
| --- | --- |
| `/rss/<channel>` | the channel's RSS feed |
| `/rss/<channel>?output_type=html` | the same posts as one HTML page — handy for checking filters in a browser |
| `/html/<channel>/<post_id>` | one post as HTML; a post from an album is shown with the whole album |
| `/json/<channel>/<post_id>` | one message as JSON: text, rendered HTML, flags, date, author, views, reactions, link preview |
| `/flags` | the list of flags the bridge assigns, as JSON (see [Filtering posts](#filtering-posts)) |
| `/ping`, `/health` | liveness and status (see [Monitoring](#monitoring)) |

`<channel>` is a public username without `@` (`DragorWW_space`) or a numeric channel id
(`-1002069358234`). The id is how you reach a private channel — the bridge account has to be
subscribed to it. To find the id, forward a post from the channel to
[@userinfobot](https://t.me/userinfobot); it replies with `Id: -100…`.
[miniflux-tg-add-bot](https://github.com/vvzvlad/miniflux-tg-add-bot) does this for you.

Single-post pages accept `?debug=true`: the page then also shows the post title and the raw
Telegram message.

### Feed parameters

All of them are query parameters and can be combined:

| Parameter | Default | Meaning |
| --- | --- | --- |
| `limit` | `50` | Maximum number of entries, 1–200. A feed often has fewer — see [below](#why-a-feed-has-fewer-entries-than-limit). |
| `exclude_flags` | — | Comma-separated flags; posts carrying any of them are dropped. See [Filtering posts](#filtering-posts). |
| `exclude_text` | — | A regular expression; posts whose text matches it are dropped. |
| `merge_seconds` | `5` | Window for time-based merging; has effect only with `TIME_BASED_MERGE=true`. See [Merging posts](#merging-posts). |
| `output_type` | `rss` | `rss` or `html`. |
| `token` | — | The access token, as an alternative to the `/<TOKEN>` path segment. |

```text
https://pgbridge.example.com/rss/DragorWW_space?limit=30&exclude_flags=advert,fwd
https://pgbridge.example.com/rss/DragorWW_space/change-me?exclude_text=розыгрыш|giveaway
https://pgbridge.example.com/rss/-1002069358234?merge_seconds=10
```

### Timeouts

A feed that is not cached yet can take a while: the bridge sends one Telegram request at a
time, with a pause between them, and media are fetched on top of that. Give your reader a
generous HTTP timeout — in Miniflux, `HTTP_CLIENT_TIMEOUT=200`. When Telegram answers with a
FloodWait, the bridge returns `429 Too Many Requests` with a `Retry-After` header (at most
190 seconds) instead of hanging.

## Filtering posts

Every feed URL carries its own filters, so the same channel can be read raw in one place and
cleaned up in another. There are two filters, and they combine: a post is dropped when it
matches **either** of them.

### By flags — `exclude_flags`

While rendering, the bridge tags each post with flags describing its content.
`exclude_flags` takes a comma-separated list of them, and a post carrying **any** of the
listed flags is dropped:

```text
/rss/DragorWW_space?exclude_flags=advert,fwd,clownpoo
```

| Flag | The post… |
| --- | --- |
| `advert` | contains ad markers: `#реклама`, `#промо`, «партнерский пост», «по промокоду», an `erid` label and the like |
| `fwd` | is forwarded from another channel or user |
| `foreign_channel` | links to another public channel (`t.me/othername`, or a boost link of another channel); links to the channel itself do not count |
| `hid_channel` | links to a private channel invite (`t.me/+…`) |
| `link` | contains an external `http(s)` link; `t.me` links do not count |
| `only_link` | is nothing but one external link, or only a link preview (gets `only_link` instead of `link`) |
| `tracking_link` | has a link with referral or tracking parameters: `utm_*`, `start=`, `invitedBy=`, `erid=` |
| `mention` | mentions an `@username` |
| `donat` | asks for donations: «донат…», `pay.cloudtips.ru`, `t.me/boost/…` links |
| `paywall` | mentions paid platforms: Boosty/Бусти, Sponsr, Дзен.Премиум |
| `stream` | announces a stream or a webinar: «стрим…», livestream, «вебинар…», «онлайн-лекция» |
| `clownpoo` | has at least 30 🤡 or at least 30 💩 reactions |
| `video` | is a video, GIF, round video or live photo with at most 200 characters of text |
| `audio` | is an audio file or a voice message with at most 200 characters of text |
| `no_image` | has no picture or video: a text-only post, a file, a poll without media… |
| `sticker` | is a sticker |
| `poll` | is a poll |
| `rich` | is a Telegram Rich Message |
| `merged` | is assembled from several messages: an album, or a time-based merge |

`/flags` returns the current list — everything above except `merged`, which is assigned
later, while grouping messages; it still works in `exclude_flags`. miniflux-tg-add-bot builds
its flag buttons from this endpoint.

Things to know:

- **Flags describe the post's own content:** its text, its links and the URL of its link
  preview. The "Forwarded from" line, the message the post replies to and the preview's title
  and description do not set `mention`, `link`, `hid_channel` or `foreign_channel`.
- **An album or merged entry carries the flags of all its parts**, so it is dropped as a
  whole if any part matches.
- **`exclude_flags=all` drops every post that has at least one flag** — far more than it
  sounds: text-only posts carry `no_image` and albums carry `merged`, so `all` empties most
  feeds. List the flags you mean instead.
- **To see which flags posts get**, set `SHOW_POST_FLAGS=true`: every entry then ends with its
  flags (`🏷 advert 🏷 link`). Together with `?output_type=html` this is the quickest way to
  tune a filter in the browser.

### By text — `exclude_text`

`exclude_text` is a regular expression
([Python syntax](https://docs.python.org/3/library/re.html#regular-expression-syntax)),
matched case-insensitively against the post's text — the message text or the media caption;
for an album, the texts of all its parts. A post in which it matches anywhere is dropped:

```text
/rss/DragorWW_space?exclude_text=розыгрыш|giveaway
/rss/DragorWW_space?exclude_text=все.*комикс|реклам.*канал
```

The second one drops posts that contain «все» followed somewhere later by «комикс», and posts
that contain «реклам» followed by «канал».

- **Alternatives are separated by `|`, not by commas.** A comma is an ordinary character in a
  regex: `реклама,акция` matches only that exact string.
- **Only the visible text is searched** — not the URLs behind hyperlinks and not the quoted
  message of a reply.
- **Encode special characters in the URL.** `+` must be written as `%2B` (unencoded, it turns
  into a space, so `\d+` silently becomes "a digit followed by a space"), `#` as `%23`, `&` as
  `%26`. Most readers
  encode Cyrillic themselves; if yours does not, use any URL encoder, e.g.
  <https://www.urlencoder.org/>.
- **A regex that does not compile fails the feed request** with an error instead of returning
  the feed unfiltered.

### Why a feed has fewer entries than `limit`

- **Filters run after `limit`.** The bridge takes the newest `limit` entries and only then
  removes the filtered ones, so an aggressive filter can leave just a few. Raise `limit` to
  compensate.
- **Albums take one message per picture.** The RSS feed reads `2 × limit` messages from the
  channel, and a 10-photo album uses ten of them, so on a channel that posts large albums
  `limit` may change little or nothing. The HTML feed (`output_type=html`) reads exactly
  `limit` messages.

## Merging posts

Telegram stores an album as separate messages, one per photo or video. The bridge always
joins them back into one entry.

Some channels post a series of separate messages instead — a picture and then its caption, a
long text in several parts. With `TIME_BASED_MERGE=true` the bridge also joins every message
published within `merge_seconds` (default 5) of the previous one into the same entry; the
window can be changed per feed with `?merge_seconds=…`. Without `TIME_BASED_MERGE` the
parameter does nothing. Merged entries get the `merged` flag.

## Access token

Whoever can reach the bridge can read through it everything your Telegram account can read —
and Telegram sees all of it as your account's activity, which sooner or later ends in limits
or a ban for botting. If the bridge is reachable from the internet, set `TOKEN`.

With `TOKEN` set, every endpoint except `/`, `/ping` and the media links requires it, as the
last path segment or as a query parameter:

```text
https://pgbridge.example.com/rss/DragorWW_space/change-me
https://pgbridge.example.com/rss/DragorWW_space?token=change-me&limit=30
```

Media links do not need the token: each one carries its own signature (see
`MEDIA_SIGNING_SECRET`).

Requests from `127.0.0.1` / `::1` skip the check. If your reverse proxy connects to the bridge
from localhost, every request would look local — put the proxy's address in
`TRUSTED_PROXIES`, and the client's real address is then taken from `X-Real-IP` /
`X-Forwarded-For`.

## Configuration

Everything is configured with environment variables. Only `TG_API_ID` and `TG_API_HASH` are
required; the bridge does not start without them.

### Basics

| Variable | Default | Description |
| --- | --- | --- |
| `TG_API_ID` | — | Telegram API id from my.telegram.org. Required. |
| `TG_API_HASH` | — | Telegram API hash from my.telegram.org. Required. |
| `PYROGRAM_BRIDGE_URL` | — | Public base URL of the bridge, e.g. `https://pgbridge.example.com`. Media links in feeds are built from it; without it they are relative (`/media/…`), and pictures load only if the reader resolves them against the bridge's address. |
| `TOKEN` | — | Access token, see [Access token](#access-token). |
| `API_PORT` | `8000` | HTTP port inside the container. |
| `API_HOST` | `0.0.0.0` | Address to listen on. |
| `TRUSTED_PROXIES` | — | Comma-separated addresses of reverse proxies whose `X-Real-IP` / `X-Forwarded-For` are trusted. |
| `TZ` | `UTC` | Time zone of the dates shown in posts. |
| `LOG_LEVEL` | `INFO` | Log level. |
| `DEBUG` | `false` | Print the raw Telegram message to stdout on every single-post request. |
| `SESSION_PATH` | `data` | Directory of the Telegram session file, relative to `/app`. |

### Feed content

| Variable | Default | Description |
| --- | --- | --- |
| `TIME_BASED_MERGE` | `false` | Merge posts published a few seconds apart, see [Merging posts](#merging-posts). |
| `SHOW_POST_FLAGS` | `false` | Show each post's flags at the end of the entry. |
| `SHOW_BRIDGE_LINK` | `false` | Add an "Open in Bridge" link to each post (the post page with `debug=true`). The link contains `TOKEN`, so everyone who reads the feed sees the token. |
| `REPLY_QUOTE_TRUNCATE_CHARS` | `200` | When a post replies to a neighbouring post of the same channel, keep only this many visible characters of the quote — otherwise the reader shows post A in full and then post B with all of A quoted inside it. `0` keeps quotes in full. Replies to other channels, to users or to older posts are never shortened. |
| `REPLY_QUOTE_TRUNCATE_DISTANCE` | `2` | How far apart (in message ids) the reply and its target may be for the quote to be shortened; 2 covers "a reply to the post right above", even after an album. `0` disables shortening. Both values apply at render time, no cache clearing needed. |

### Media links

| Variable | Default | Description |
| --- | --- | --- |
| `MEDIA_SIGNING_SECRET` | — | Secret for signing media URLs (HMAC-SHA256, key derived with HKDF). If unset, the key is derived from `TOKEN`, and without `TOKEN` it comes from a key file generated in the data volume. Set this or `TOKEN`: the key file disappears with the volume, and every media URL already delivered to readers dies with it. |
| `MEDIA_ALLOW_LEGACY_DIGEST` | `true` | Also accept media URLs signed by the old scheme (SHA-1, 8 characters), so links delivered before an upgrade keep working. Set to `false` once every feed has been re-read. |
| `MEDIA_URL_TTL_DAYS` | — | Give media URLs a signed expiry this many days out. Unset means no expiry, which is right for RSS: readers often fetch an entry's media days later. |

### Telegram connection

| Variable | Default | Description |
| --- | --- | --- |
| `TG_PROXY_HOST` | — | Connect to Telegram through a SOCKS5 proxy (e.g. an MTProto proxy's SOCKS5 interface). Prefer an IP address: a hostname is re-resolved on every reconnect. |
| `TG_PROXY_PORT` | `1080` | Proxy port. |
| `TG_PROXY_USERNAME`, `TG_PROXY_PASSWORD` | — | Proxy credentials, if needed. |
| `TG_RPC_CONCURRENCY` | `1` | How many Telegram requests may run at once. |
| `TG_RPC_MIN_INTERVAL_MS` | `500` | Minimum pause between the starts of two Telegram requests, ms. |
| `TG_RPC_TIMEOUT` | `60` | Maximum duration of one Telegram request, seconds. |
| `TG_CHAT_CACHE_TTL_HOURS` | `12` | How long channel info (title, username, id) is cached, hours. |

### Watchdog

The watchdog periodically checks that the Telegram session is actually alive and restarts it
in-process when it is not.

| Variable | Default | Description |
| --- | --- | --- |
| `TG_WATCHDOG_ENABLED` | `true` | Turn the watchdog on or off. |
| `TG_WATCHDOG_INTERVAL` | `60` | Seconds between checks. |
| `TG_WATCHDOG_TIMEOUT` | `10` | Timeout of one check, seconds. |
| `TG_WATCHDOG_FAILURES` | `3` | Consecutive failed checks before a restart. |
| `TG_WATCHDOG_RESTART_TIMEOUT` | `90` | Timeout of the restart itself, seconds. |
| `TG_WATCHDOG_HEARTBEAT_EVERY` | `30` | Log an INFO heartbeat every N successful checks. |
| `TG_DISCONNECT_FLAP_LIMIT` | `3` | Disconnects within the window that trigger a restart. |
| `TG_DISCONNECT_FLAP_WINDOW` | `120` | That window, seconds. |
| `TG_PING_UNHEALTHY_AFTER` | `250` | `/ping` reports unhealthy when the last successful check is older than this many seconds. The default is derived from the three values above: interval × (failures + 1) + timeout. |

### Media downloads and cache

| Variable | Default | Description |
| --- | --- | --- |
| `MEDIA_DOWNLOAD_TIMEOUT_MIN` | `120` | Download timeout for regular files, and the floor for large ones, seconds. |
| `MEDIA_DOWNLOAD_TIMEOUT_MAX` | `1800` | Download timeout cap for the largest videos, seconds. |
| `MEDIA_DOWNLOAD_MIN_SPEED` | `262144` | Assumed minimum download speed, bytes/s; a large file's timeout ≈ size / this speed, clamped to the two values above. |
| `TG_MAX_CONCURRENT_TRANSMISSIONS` | `3` | Simultaneous file downloads from Telegram. |
| `MEDIA_TIMEOUT_RESTART_THRESHOLD` | `5` | Consecutive download timeouts after which the Telegram client is restarted (the watchdog cannot see a dead media connection). |
| `MEDIA_BACKOFF_MAX_S` | `21600` | Upper bound of the growing pause before re-trying a file that keeps failing to download, seconds. |
| `MEDIA_FAILURES_DROP_ROW` | `15` | Failed downloads in a row after which a file is forgotten. |
| `CACHE_SWEEP_INTERVAL` | `900` | Seconds between passes of the background media downloader and cache cleaner; at least 60. |
| `IO_THREAD_POOL_SIZE` | `32` | Threads for blocking I/O (SQLite, file system). |

### Caching and delays

- A channel's history is cached for 6.4–8 hours (8 hours with a per-channel jitter), so a new
  post can take that long to appear in its feed. This is what keeps the account's request rate
  to Telegram low.
- Media referenced by rendered posts are downloaded in the background and kept in the data
  volume; a file nobody has requested for 20 days is deleted.
- Feeds are sent with `Cache-Control: private, max-age=300` and answer conditional requests
  with `304 Not Modified`.

## Monitoring

- **`/ping`** — instant liveness check for the container healthcheck; never calls Telegram.
  Answers `200 {"status": "ok", …}` while the session is connected and the watchdog's last
  successful check is fresh, `503 {"status": "degraded", …}` otherwise. The repository's
  [`docker-compose.yml`](docker-compose.yml) shows the healthcheck.
- **`/health`** (requires the token) — calls Telegram and returns JSON with the logged-in
  account, the configuration (secrets masked), media cache statistics and render-failure
  counters.

## Upgrading

The container starts as root on purpose: the entrypoint first hands the data volume
(`/app/data`) to uid 1000 and then drops to that user to run the service. So an old install
whose volume still holds root-owned files upgrades without any manual steps.

If you pin `user: "1000:1000"` (the uid of the image's `app` user) in the compose file, the
container starts as that user and leaves the volume as it is. An old volume with root-owned files then has to be
handed over once by hand (the real volume name is in `docker volume ls`):

```bash
docker run --rm -v <stack>_pyrogram_bridge:/data busybox chown -R 1000:1000 /data
```

## Development

Python 3.11; `python-magic` needs the system `libmagic`.

```bash
pip install -r requirements.txt
pytest
```

Gitea Actions runs the tests on pull requests to `main`; a push to `main` runs them again and,
if they pass, builds and publishes the image `gitea.vvzvlad.xyz/projects/pyrogram-bridge:latest`.
