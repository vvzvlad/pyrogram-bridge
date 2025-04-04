# Pyrogram Bridge

## First start

1)Login at https://my.telegram.org/apps, API development tools - create new application, copy api_id and api_hash

1) Сreate docker-compose.yml (or, recommended, create stack in portainer):

```docker-compose
volumes:
  pyrogram_bridge:
  
services:
  pyrogram_bridge:
    image: ghcr.io/vvzvlad/pyrogram-bridge:latest
    container_name: pyrogram-bridge
    environment:
      TG_API_ID: 290389758
      TG_API_HASH: c22987sdfnkjjhd37efa5f0
      PYROGRAM_BRIDGE_URL: https://pgbridge.example.com
      API_PORT: 80
      TOKEN: "1234567890"
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

2) Run ```docker-compose up -d``` or start stack in portainer

3) Enter in container:

```bash
docker exec -it pyrogram-bridge /bin/bash
```

4) Run bridge in interactive mode:

```bash
python3 api_server.py
```

5) Enter phone number, get code in telegram, enter code, and copy session string. 

```text
Enter phone number or bot token: +7 993 850 5104
Is "+7 993 850 5104" correct? (y/N): y
The confirmation code has been sent via Telegram app
Enter confirmation code: 69267
The two-step verification is enabled and a password is required
Password hint: None
Enter password (empty to recover): Passport-Vegan-Scale6
```

Wait until show message "INFO:     Application startup complete.", then exit from container.

6) Restart bridge container:

```bash
docker restart pyrogram-bridge
```

Session file will be saved in your data directory, in docker compose case — /var/lib/docker/volumes/pyrogram_bridge/_data/pyro_bridge.session.  

## ENV Settings 

TG_API_ID - telegram api id  
TG_API_HASH - telegram api hash  
API_PORT - port to run http server  
PYROGRAM_BRIDGE_URL - url to rss bridge, used for generate absolute url to media  
TOKEN - optional, if set, will be used to check if user has access to rss feed. If token is set, rss url will be https://pgbridge.example.com/rss/DragorWW_space/1234567890  
Use this if you rss bridge access all world, otherwise your bridge can be used by many people and telegram will inevitably be sanctioned for botting.  
TIME_BASED_MERGE - optional, if set to true, will merge posts by time. Merge time is 5 seconds, use &merge_seconds=XX in rss url for tuning.  
SHOW_BRIDGE_LINK - optional, if set to true, will add "Open in Bridge" link to html post view. Default is false.  
SHOW_POST_FLAGS - optional, if set to true, will show post flags in html post view. Default is false.  

## Get channel rss feed (use it in your rss reader)

``` curl https://pgbridge.example.com/rss/DragorWW_space ```  
``` curl https://pgbridge.example.com/rss/DragorWW_space/1234567890 ``` with auth token (if env token is set)  
``` curl https://pgbridge.example.com/rss/DragorWW_space?limit=30 ``` with limit parameter (also, can be used with token)  
``` curl https://pgbridge.example.com/rss/DragorWW_space?merge_seconds=10 ``` with merge_seconds parameter  
``` curl https://pgbridge.example.com/rss/DragorWW_space?exclude_flags=video,stream,donat,clown ``` with exclude_flags parameter  
``` curl https://pgbridge.example.com/rss/DragorWW_space?exclude_text=реклама,акция ``` with exclude_text parameter  
``` curl https://pgbridge.example.com/rss/DragorWW_space?exclude_text="специальное предложение",акция ``` with exclude_text parameter containing phrases with spaces  
``` curl https://pgbridge.example.com/html/DragorWW_space/123?debug=true ``` with debug parameter to print raw message to logs  

Warning: TG API has rate limit, and bridge will wait time before http response, if catch FloodWait exception. Increase http timeout in your client prevention timeout error. Examply, in miniflux: ENV HTTP_CLIENT_TIMEOUT=200  

Note: bridge has support for numeric channel ID: use id (e.g. -1002069358234) instead of username (e.g. DragorWW_space) for rss/html/json urls: ``` curl https://pgbridge.example.com/rss/-1002069358234 ```  
Obviously, you must have a closed/hidden channel subscription prior to use, using the same Telegram account you got the token from (see Get session). Bridge doesn't do anything supernatural, it just pretends to be a TG client. If you don't have access to the channel, you can't get anything from it.  

For known id channel, you can use bot @userinfobot: forward message from channel to bot, and get id from bot response: "Id: -1002069358234".  
Or, use my https://github.com/vvzvlad/miniflux-tg-add-bot to add channel to miniflux: after forward message from channel to bot, subscribition automatically added to miniflux.

"Limit" parameter can work somewhat unintuitively: this will be noticeable on groups that post multiple sets of pictures.  
The thing is that in Telegram each picture is a separate message and they are grouped later, on the client. The maximum number of pictures in one message is 10, so in order to guarantee 10 posts with 10 pictures with a limit of 10 posts, we would need to request 10х10=100 messages each time.  
This creates an unnecessary load, so we do something else: we request limit х2 and delete the last group of media files for fear that it might be incomplete (because to find out for sure, we have to go further down the history and find the next group). So don't expect the limit parameter to give you exactly as many posts as it specifies, they may be a) much less b)the number of posts may not change when the limit is changed, because incomplete groups at the end of the feed are deleted automatically

## Exclude flags

Exclusion flags are a way to filter channel content based on pre-defined (by me) criteria. It's not a universal regexp-based filtering engine, for example, but it does 99% of my tasks of filtering the content of some toxic tg channels (mostly with fresh memes).  

There are several flags:  

- video - presence of video and small text in the post  
- stream - words like "стрим", "livestream"  
- donat - word "донат" and its variations  
- clown - clown emoticon (🤡) in post reactions (>30)  
- poo - poo emoticon (💩) in post reactions (>30)  
- advert - "#реклама" tag, "Партнерский пост" or "по промокоду" phrases  
- fwd - forwarded messages from channels, users or hidden users  
- hid_channel - links to closed tg channels (https://t.me/+S0OfKyMDRi)  
- foreign_channel - links to open channels (https://t.me/superchannel) that do not equal the name of the current channel
- link - presence of any http/https links in the post
- mention - presence of @username mentions in the post
- vebinar - words like "вебинар" and its variations

You can use exclude_flags parameter in rss/html/json urls to exclude posts with certain flags. For example, to exclude all posts with the flags "video", "stream", "donat", "clown", you can use:

``` curl https://pgbridge.example.com/rss/DragorWW_space?exclude_flags=video,stream,donat,clown ```

Or use meta-flag "all" to exclude all flags in posts:

``` curl https://pgbridge.example.com/rss/DragorWW_space?exclude_flags=all ```

## Exclude text

You can filter out posts containing specific text patterns using the `exclude_text` parameter. This parameter accepts a regular expression pattern that will be matched against the post text.

The pattern is case-insensitive and supports all standard regex features. For example:

``` curl https://pgbridge.example.com/rss/DragorWW_space?exclude_text=все.*комикс|реклам.*канал ```

This will exclude posts containing:

- Any text starting with "все" and ending with "комикс"
- Any text containing "реклам" followed by "канал"

You can use any regex pattern, including:

- `.*` for any characters
- `\d+` for numbers
- `[а-яА-Я]+` for Russian letters
- `|` for alternative patterns
- And other standard regex features

The pattern is matched against the entire post text, so you can create complex filtering rules.

For some applications you will need to convert the request to an encoded URL: 
```Салли.*и.*Фасолька|Увядший.*Лепесток``` -> ```%D0%A1%D0%B0%D0%BB%D0%BB%D0%B8.%2A%D0%B8. %2A%D0%A4%D0%B0%D1%81%D0%BE%D0%BB%D1%8C%D0%BA%D0%B0%7C%D0%A3%D0%B2%D1%8F%D0%B4%D1%88%D0%B8%D0%B9.%2A%D0%9B%D0%B5%D0%BF%D0%B5%D1%81%D1%82%D0%BE%D0%BA```
For example, you can use this tool: https://www.urlencoder.org/