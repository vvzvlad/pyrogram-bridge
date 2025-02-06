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

## Get channel messages

``` curl https://pgbridge.example.com/html/DragorWW_space/87 ```
``` curl https://pgbridge.example.com/json/DragorWW_space/87 ```
