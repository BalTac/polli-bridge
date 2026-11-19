# Deployment kit

The exact configuration behind the live demo at <https://aibridge.bot.nu>: a public
**BYOP** gateway (no server key), a landing page that explains it, and traffic
statistics that store no personal data.

| file | what it is |
|---|---|
| `docker-compose.yml` | bridge (not published) + Caddy on 80/443 |
| `Caddyfile` | TLS, rate limit 60/min + burst 20, JSON log with the query string stripped |
| `caddy.Dockerfile` | Caddy compiled with `mholt/caddy-ratelimit` via xcaddy |
| `collector/collector.py` | Caddy log → masked IPs → SQLite → `stats.html` + `stats.json` |
| `systemd/` | timer running the collector every 5 minutes |
| `site/` | landing page, `llms.txt`, `/.well-known/ai-plugin.json`, `robots.txt`, `security.txt` |

Full step-by-step: **[DEPLOY.md](../DEPLOY.md)**.
