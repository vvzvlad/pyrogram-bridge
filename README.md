# Pyrogram Bridge

## Create API ID/HASH

1)Login at https://my.telegram.org/apps

2)API development tools

3)Create new application

4)Copy App api_id and api_hash  


## Get session

1)python3 -m venv .venv

2)source .venv/bin/activate

3)pip install pyrogram

4)Run ```TG_API_ID=290389758 TG_API_HASH=c22987sdfnkjjhd37efa5f0 python3 session_generator.py```

5)Enter phone number, get code in telegram, enter code, and copy session string:

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

## Get channel rss feed

``` curl https://pgbridge.example.com/rss/DragorWW_space ```

or

## Get channel messages

``` curl https://pgbridge.example.com/html/DragorWW_space/87 ```
``` curl https://pgbridge.example.com/json/DragorWW_space/87 ```
