# Caddy con il plugin rate_limit, compilato nel binario.
# Caddy non carica moduli a runtime: xcaddy build e' il modo ufficiale.
FROM caddy:2.10-builder AS builder
RUN xcaddy build \
    --with github.com/mholt/caddy-ratelimit

FROM caddy:2.10
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
