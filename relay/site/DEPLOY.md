# Deploying the 9Bot Install Pages

Static pages served by nginx at `https://1453.life/install`.

## Files

```
relay/site/
  install.html     # Installation guide — /install
  guide.html       # Quick Start Guide — /install/guide
  img/
    friend_marker.png
    target_marker.png
```

## Deploy

```bash
# Upload files to droplet
scp -r relay/site root@104.236.8.9:/opt/9bot-relay/

# SSH in and add nginx config (one-time)
ssh root@104.236.8.9
```

Add to `/etc/nginx/sites-available/9bot-relay` inside the `server` block:

```nginx
    # Static install pages
    location = /install {
        alias /opt/9bot-relay/site/install.html;
        default_type text/html;
    }
    location /install/ {
        alias /opt/9bot-relay/site/;
        default_type text/html;
        try_files $uri $uri.html =404;
    }
```

Then reload:
```bash
nginx -t && systemctl reload nginx
```

## URLs

- `https://1453.life/install` — Installation Guide (send this to users)
- `https://1453.life/install/guide` — Quick Start Guide

## Update

Just re-upload the files — no service restart needed:
```bash
scp -r relay/site root@104.236.8.9:/opt/9bot-relay/
```

## Ko-fi

Ko-fi page: https://ko-fi.com/ninebot
