# Deployment kit — public BYOP instance

This is the exact configuration behind the live demo at **https://aibridge.bot.nu**:
a public gateway with **no key of its own**, a landing page that explains it, and
privacy-preserving traffic statistics.

Copy this folder to a server with Docker and change the domain. Everything else is
already tuned for a small VPS (1 vCPU / 1 GB is plenty).

```
deploy/
├─ docker-compose.yml        bridge + Caddy, nothing else exposed
├─ Caddyfile                 TLS, rate limit 60/min + burst 20, JSON access log
├─ caddy.Dockerfile          Caddy with the rate_limit plugin compiled in
├─ collector/collector.py    log → masked IPs → SQLite → stats.html + stats.json
├─ systemd/                  timer: one collector run every 5 minutes
└─ site/                     landing page, llms.txt, /.well-known/, robots.txt
```

## 1. Prerequisites

- Ubuntu 24.04 (or any distro with Docker and systemd)
- a DNS A record for your domain pointing at the server
- ports 80 and 443 open (80 is needed for the ACME challenge)

## 2. Layout

```bash
sudo mkdir -p /srv/aibridge && cd /srv/aibridge
git clone https://github.com/BalTac/polli-bridge app
cp -r app/deploy/{docker-compose.yml,Caddyfile,caddy.Dockerfile,collector,systemd,site} .
mv caddy.Dockerfile caddy/Dockerfile 2>/dev/null || (mkdir -p caddy && mv caddy.Dockerfile caddy/Dockerfile)
mkdir -p data/logs caddy/data caddy/config
```

Set the domain (two places) and the ACME email:

```bash
sed -i 's/aibridge\.bot\.nu/your.host.example/g; s/admin@bot\.nu/you@example.com/g' Caddyfile site/index.html site/llms.txt site/.well-known/*.txt site/.well-known/*.json
```

## 3. The bridge in BYOP mode

```bash
cat > app/.env <<'EOF'
# No server key on purpose: this instance is bring-your-own-key.
POLLINATIONS_API_TOKEN=
SERVER_HOST=0.0.0.0
SERVER_PORT=8083
LOG_LEVEL=INFO
LOCAL_API_TOKEN=
EOF
chmod 600 app/.env
```

Leaving `POLLINATIONS_API_TOKEN` empty is what makes the bridge BYOP: it refuses
requests without a caller key (401) and never spends anyone's balance but the caller's.
See the "Security" section of the main README for the full matrix.

## 4. Start

```bash
docker compose build          # xcaddy compiles Caddy + rate_limit: 3-10 min on 1 vCPU
docker compose up -d
docker compose logs -f caddy  # wait for "certificate obtained successfully"
```

Verify:

```bash
curl https://your.host.example/health                 # {"auth":"byop", ...}
curl -X POST https://your.host.example/v1/chat/completions \
  -H "Content-Type: application/json" -d '{"model":"openai","messages":[]}'   # 401
```

## 5. Statistics

The collector reads Caddy's JSON access log, **masks the IP immediately**
(`/24` for IPv4, `/64` for IPv6), resolves the country offline with
[DB-IP Lite](https://db-ip.com) (CC BY 4.0), aggregates into SQLite and writes
`site/stats.html` + `site/stats.json`.

```bash
sudo install -m 755 collector/collector.py /srv/aibridge/collector/collector.py
sudo install -m 644 systemd/aibridge-collector.service /etc/systemd/system/
sudo install -m 644 systemd/aibridge-collector.timer   /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now aibridge-collector.timer
```

It needs read access to the access log, which Caddy writes as root mode 0600, so the
timer runs as root (it only reads logs and writes two files in `site/`).

Retention: raw logs 7 days (rotated files older than that are deleted), per-minute
aggregates 30 days, per-day unique masks 7 days.

### What is deliberately not collected

- no prompts or completions: Caddy's access log records the URI, and a Caddy
  `format filter` strips the **entire query string** before writing. A GET
  `/generate?prompt=...` ends up in the log as `/generate`.
- no keys: Caddy redacts `Authorization` and `Cookie` values as `REDACTED` by default;
  verified by sending a marker key and grepping the log for it (0 hits).
- no cookies, no fingerprinting, no third-party analytics, no IP kept in the database.
- the path is reduced to a fixed group (`/v1/chat/completions`, `/v1/audio`, …) and
  dynamic prompts in the path (`/video/{prompt}`) are collapsed, so user content
  cannot leak through the path either.

## 6. Caveats worth knowing

- **413 on oversized uploads shows up as a connection reset** in some clients: Caddy
  enforces `request_body max_size 25MB` and closes while the client is still
  uploading. The access log records `413`; curl may report `http=000`.
- The rate limit is a sliding window: `events 80 window 1m` = 60 sustained + 20 burst.
  It is per IP, so everyone behind the same NAT shares it.
- Caddy writes the log as root `0600`; anything that reads it needs root or a log
  shipper with privileges.
- The bridge image is ~900 MB (Python + all media deps). It builds fine on 1 GB of RAM
  if you have swap.
