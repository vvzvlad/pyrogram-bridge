# Pyrogram Bridge

## Create API ID/HASH

1)Login at https://my.telegram.org/apps  
2)API development tools  
3)Create new application  
4)Copy App api_id and api_hash  


## Get session

1)```python3 -m venv .venv && source .venv/bin/activate```
2)```pip install pyrogram```
3)Run ```TG_API_ID=290389758 TG_API_HASH=c22987sdfnkjjhd37efa5f0 python3 session_generator.py```  
4)Enter phone number, get code in telegram, enter code, and copy session string:  

```text
Enter phone number or bot token: +7 993 850 5104
Is "+7 993 850 5104" correct? (y/N): y
The confirmation code has been sent via Telegram app
Enter confirmation code: 69267
The two-step verification is enabled and a password is required
Password hint: None
Enter password (empty to recover): Passport-Vegan-Scale6

Your session string:
========================================
AgG7QBoANg0YVmwZTZmqadO4MJQdnaRRnXwSYpbbkGf49aATvTZj-yKcvdH8IsIDbwp00PbWFcbSjzoPsmiUK8BF80yVmML7iEQptBZrRTLsnoxmeglD-I1dqcAB2ufkxQDM_40y5KAHiFvzAzXhVngQo8W7u3ZPQXb_DTcfogXXePPBQAyV20cuwGrsArv-R39ssSWnFueGnB21Y_cTXTQAAAAFPEaL8AA
========================================
Use session on ENV variable TG_SESSION_STRING in docker-compose.yml
```

6)Set session ENV variable in docker-compose.yml file:

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
      TG_SESSION_STRING: "AgG7QBoANg0YVmwZTZmqadO4MJQdn............FPEaL8AA"
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

PYROGRAM_BRIDGE_URL - url to rss bridge, used for generate absolute url to media
TOKEN - optional, if set, will be used to check if user has access to rss feed. If token is set, rss url will be https://pgbridge.example.com/rss/DragorWW_space/1234567890
Use this if you rss bridge access all world, otherwise your bridge can be used by many people and telegram will inevitably be sanctioned for botting.

## Get channel rss feed (use it in your rss reader)

``` curl https://pgbridge.example.com/rss/DragorWW_space ``` 
``` curl https://pgbridge.example.com/rss/DragorWW_space/1234567890 ``` with auth token (if env token is set)
``` curl https://pgbridge.example.com/rss/DragorWW_space?limit=30 ``` with limit parameter (also, can be used with token)

Warning: TG API has rate limit, and bridge will wait time before http response, if catch FloodWait exception. Increase http timeout in your client prevention timeout error. Examply, in miniflux: ENV HTTP_CLIENT_TIMEOUT=200  

Note: bridge has support for numeric channel ID: use id (e.g. -1002069358234) instead of username (e.g. DragorWW_space) for rss/html/json urls: ``` curl https://pgbridge.example.com/rss/-1002069358234 ```  
Obviously, you must have a closed/hidden channel subscription prior to use, using the same Telegram account you got the token from (see Get session). Bridge doesn't do anything supernatural, it just pretends to be a TG client. If you don't have access to the channel, you can't get anything from it.  

For known id channel, you can use bot @userinfobot: forward message from channel to bot, and get id from bot response: "Id: -1002069358234".  
Or, use my https://github.com/vvzvlad/miniflux-tg-add-bot to add channel to miniflux: after forward message from channel to bot, subscribition automatically added to miniflux.

"Limit" parameter can work somewhat unintuitively: this will be noticeable on groups that post multiple sets of pictures.  
The thing is that in Telegram each picture is a separate message and they are grouped later, on the client. The maximum number of pictures in one message is 10, so in order to guarantee 10 posts with 10 pictures with a limit of 10 posts, we would need to request 10*10=100 messages each time.  
This creates an unnecessary load, so we do something else: we request limit*2 and delete the last group of media files for fear that it might be incomplete (because to find out for sure, we have to go further down the history and find the next group). So don't expect the limit parameter to give you exactly as many posts as it specifies, they may be a) much less b)the number of posts may not change when the limit is changed, because incomplete groups at the end of the feed are deleted automatically

## Get channel messages

``` curl https://pgbridge.example.com/html/DragorWW_space/87 ```
``` curl https://pgbridge.example.com/json/DragorWW_space/87 ```

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

You can use exclude_flags parameter in rss/html/json urls to exclude posts with certain flags. For example, to exclude all posts with the flags "video", "stream", "donat", "clown", you can use:

``` curl https://pgbridge.example.com/rss/DragorWW_space?exclude_flags=video,stream,donat,clown ```

Or use meta-flag "all" to exclude all flags in posts:

``` curl https://pgbridge.example.com/rss/DragorWW_space?exclude_flags=all ```

