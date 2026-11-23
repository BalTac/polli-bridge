#!/usr/bin/env python3
"""aibridge collector — statistiche di traffico senza dati personali.

Legge i log JSON di Caddy, maschera subito l'IP (/24 IPv4, /64 IPv6) e tiene
solo aggregati in SQLite. Nessun prompt, nessun header, nessuna chiave:
il path viene ridotto a un gruppo fisso, la query string non viene mai letta.

Uso:
    collector.py            # un giro: consuma i log nuovi, aggiorna stats.html/json
    collector.py --geo      # scarica e ricostruisce la tabella paesi (DB-IP Lite)
    collector.py --geo --force   # idem, ignorando la cache del mese corrente

Solo standard library.
"""
from __future__ import annotations

import base64
import bisect
import gzip
import hashlib
import io
import ipaddress
import json
import math
import os
import re
import secrets
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path("/srv/aibridge")
LOG_DIR = BASE / "data" / "logs"
DB_PATH = BASE / "data" / "stats.db"
GEO_FILE = BASE / "collector" / "country_ranges.json"
STATE_FILE = BASE / "collector" / "state.json"
OUT_HTML = BASE / "site" / "stats.html"
OUT_JSON = BASE / "site" / "stats.json"
# Indicatori interni: NON finiscono in site/ (pubblicata), restano in data/.
INDICATORS = BASE / "data" / "indicators.json"
HLL_FILE = BASE / "data" / "hll.json"
SEED_FLAG = BASE / "data" / "seed-hll.flag"   # one-shot: semina lo sketch dallo storico
KINDS_FLAG = BASE / "data" / "backfill-kinds.flag"   # one-shot: ricostruisce kind_day dai log

# HyperLogLog: 16384 registri da 6 bit -> errore standard ~0.8%.
# Conta visitatori unici *all time* senza conservare nessun identificatore:
# si hasha l'IP gia' mascherato con un salt privato, e nel file restano solo
# i registri (byte), non gli hash. Senza il salt non si puo' risalire a nulla.
HLL_P = 14
HLL_M = 1 << HLL_P

# Classificazione del client dallo user agent. L'ordine conta: gli agenti AI
# spesso si travestono da browser, e i crawler generici contengono "bot".
KIND_RULES = (
    ("ai-agent", re.compile(r"(claude-user|exa\w*bot|gptbot|oai-searchbot|perplexity|ccbot|anthropic-ai|cohere-ai|youbot|bytespider|amazonbot|applebot)", re.I)),
    ("crawler", re.compile(r"(bot\b|crawler|spider|slurp|scrap|leakix|l9scan|scan\b|httpclient|headless)", re.I)),
    ("http-tool", re.compile(r"^(curl|wget|python-requests|python-urllib|httpx|axios|node-fetch|okhttp|java/|go-http-client)", re.I)),
    ("browser", re.compile(r"mozilla/5\.0.*(chrome|safari|firefox|edg/|opr/)", re.I)),
)


def kind_of(ua: str) -> str:
    ua = (ua or "").strip()
    if not ua:
        return "other"
    for name, rx in KIND_RULES:
        if rx.search(ua):
            return name
    return "other"


def hll_add(regs: bytearray, key: bytes) -> None:
    h = int.from_bytes(hashlib.sha1(key).digest()[:8], "big")
    idx = h >> (64 - HLL_P)
    rest = h & ((1 << (64 - HLL_P)) - 1)
    rho = (64 - HLL_P) - rest.bit_length() + 1
    if rho > regs[idx]:
        regs[idx] = rho


def hll_count(regs: bytearray) -> int:
    m = HLL_M
    alpha = 0.7213 / (1.0 + 1.079 / m)
    s = 0.0
    for r in regs:
        s += 2.0 ** -r
    e = alpha * m * m / s
    if e <= 2.5 * m:                      # range piccolo: linear counting
        v = regs.count(0)
        if v:
            return int(round(m * math.log(m / v)))
    return int(round(e))


def backfill_kinds(conn: sqlite3.Connection, geo) -> tuple[int, int]:
    """Ricostruisce kind_day e gli sketch per tipo leggendo TUTTI i log disponibili.

    Il collector consuma i log in avanti (state.json tiene gli offset), quindi senza
    questo la distinzione bot/crawler/umano partirebbe da zero. Toccata solo kind_day.
    """
    store = load_hll()
    regs = {name: hll_regs(store, name) for name in ("all", "ai-agent", "crawler", "http-tool", "browser", "other")}
    counts: dict[tuple[str, str], int] = {}
    lines = 0
    for path in sorted(LOG_DIR.glob("access*.log")):
        try:
            fh = path.open("rb")
        except OSError:
            continue
        with fh:
            for raw in fh:
                try:
                    entry = json.loads(raw)
                except Exception:
                    continue
                req = entry.get("request") or {}
                if not req:
                    continue
                when = datetime.fromtimestamp(float(entry.get("ts") or 0), timezone.utc)
                day = when.strftime("%Y-%m-%d")
                ua = ((req.get("headers") or {}).get("User-Agent") or [""])[0]
                kind = kind_of(ua)
                counts[(day, kind)] = counts.get((day, kind), 0) + 1
                masked, _v = mask_ip(req.get("remote_ip") or "")
                if masked and masked != "invalid":
                    hll_add(regs.setdefault(kind, bytearray(HLL_M)),
                            (store["salt"] + "|" + kind + "|" + masked).encode())
                lines += 1
    conn.execute("DELETE FROM kind_day")
    for (day, kind), n in counts.items():
        conn.execute("INSERT INTO kind_day(day, kind, count) VALUES(?,?,?)", (day, kind, n))
    save_hll(store, regs)
    return lines, len(counts)


def seed_hll_from_uniques(conn: sqlite3.Connection, store: dict) -> int:
    """Semina lo sketch 'all' con gli IP mascherati gia' aggregati in `uniques`.

    Serve una volta sola: il collector consuma i log in avanti, quindi senza questa
    semina l'all-time partirebbe da zero ignorando i giorni già raccolti.
    """
    regs = hll_regs(store, "all")
    n = 0
    for (ip_mask,) in conn.execute("SELECT ip_mask FROM uniques"):
        if ip_mask and ip_mask != "invalid":
            hll_add(regs, (store["salt"] + "|" + ip_mask).encode())
            n += 1
    merged = {name: hll_regs(store, name) for name in store.get("sketches", {})}
    merged["all"] = regs
    save_hll(store, merged)
    return n


def load_hll() -> dict:
    """Sketch persistenti: {"salt": hex, "sketches": {kind: base64(registers)}}."""
    if HLL_FILE.exists():
        try:
            d = json.loads(HLL_FILE.read_text(encoding="utf-8"))
            if d.get("salt") and isinstance(d.get("sketches"), dict):
                return d
        except Exception:
            pass
    return {"salt": secrets.token_hex(16), "sketches": {}}


def hll_regs(store: dict, name: str) -> bytearray:
    raw = store["sketches"].get(name)
    if raw:
        b = base64.b64decode(raw)
        if len(b) == HLL_M:
            return bytearray(b)
    return bytearray(HLL_M)


def save_hll(store: dict, regs: dict[str, bytearray]) -> None:
    store["sketches"] = {k: base64.b64encode(bytes(v)).decode("ascii") for k, v in regs.items()}
    store["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tmp = HLL_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(store), encoding="utf-8")
    tmp.replace(HLL_FILE)
    try:
        os.chmod(HLL_FILE, 0o644)
    except OSError:
        pass

LOCAL_TZ = timezone(timedelta(hours=1))  # Europe/Rome (CET); solo per le etichette
RAW_RETENTION_DAYS = 7
MINUTE_RETENTION_DAYS = 30
UNIQUE_RETENTION_DAYS = 7

# Gruppi di path: qualunque cosa non elencata finisce in "other".
STATIC_OR_API = [
    (re.compile(r"^/v1/chat/completions$"), "/v1/chat/completions"),
    (re.compile(r"^/v1/models$"), "/v1/models"),
    (re.compile(r"^/v1/images/(generations|edits)$"), "/v1/images"),
    (re.compile(r"^/v1/audio/(speech|transcriptions)(/with-timestamps)?$"), "/v1/audio"),
    (re.compile(r"^/v1/embeddings$"), "/v1/embeddings"),
    (re.compile(r"^/v1/jobs/[^/]+(/result)?$"), "/v1/jobs"),
    (re.compile(r"^/health(/transcription)?$"), "/health"),
    (re.compile(r"^/docs"), "/docs"),
    (re.compile(r"^/redoc"), "/redoc"),
    (re.compile(r"^/openapi\.json$"), "/openapi.json"),
    (re.compile(r"^/stats(\.json|\.html)?$"), "/stats"),
    (re.compile(r"^/(llms\.txt|robots\.txt|\.well-known/.+)$"), "/static-meta"),
    (re.compile(r"^/(video|3d)/.+$"), r"/\1/{prompt}"),
    (re.compile(r"^/generate$"), "/generate"),
    (re.compile(r"^/text/.+$"), "/text/*"),
    (re.compile(r"^/audio/.+$"), "/audio/*"),
    (re.compile(r"^/models/.+$"), "/models/*"),
    (re.compile(r"^/account/.+$"), "/account/*"),
    (re.compile(r"^/$"), "/"),
]


def group_of(path: str) -> str:
    for rx, name in STATIC_OR_API:
        if rx.match(path):
            return name.replace("\\1", path.split("/")[1]) if "\\1" in name else name
    return "other"


def mask_ip(raw: str) -> tuple[str, int]:
    """Ritorna (ip_mascherato, versione). Mai l'IP completo verso il database."""
    try:
        ip = ipaddress.ip_address(raw.strip().strip("[]"))
    except ValueError:
        return "invalid", 4
    if ip.version == 4:
        return str(ipaddress.ip_network(f"{ip}/24", strict=False).network_address), 4
    return str(ipaddress.ip_network(f"{ip}/64", strict=False).network_address), 6


def client_family(ua: str) -> str:
    ua = (ua or "").strip()
    if not ua:
        return "none"
    low = ua.lower()
    for needle, name in (
        ("openai-python", "openai-python"),
        ("openai-node", "openai-node"),
        ("openai/", "openai-sdk"),
        ("curl", "curl"),
        ("python-requests", "python-requests"),
        ("python-urllib", "python-urllib"),
        ("httpx", "httpx"),
        ("node", "node"),
        ("go-http-client", "go"),
        ("okhttp", "okhttp"),
        ("libwww-perl", "perl"),
        ("wget", "wget"),
        ("postman", "postman"),
        ("mozilla", "browser"),
    ):
        if needle in low:
            return name
    return "other"


# --------------------------------------------------------------------------- geo
def load_geo() -> tuple[list[int], list[int], list[list]]:
    if not GEO_FILE.exists():
        return [], [], [[], []]
    data = json.loads(GEO_FILE.read_text())
    return list(data["v4_start"]), list(data["v6_start"]), [data["v4"], data["v6"]]


def country_of(raw_ip: str, geo) -> str:
    v4_start, v6_start, tables = geo
    try:
        ip = ipaddress.ip_address(raw_ip.strip().strip("[]"))
    except ValueError:
        return "??"
    if ip.version == 4:
        starts, table = v4_start, tables[0]
    else:
        starts, table = v6_start, tables[1]
    if not starts:
        return "??"
    value = int(ip)
    idx = bisect.bisect_right(starts, value) - 1
    if idx < 0:
        return "??"
    end, cc = table[idx]
    return cc if value <= end else "??"


def build_geo(force: bool = False) -> None:
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m")
    if GEO_FILE.exists() and not force:
        try:
            if json.loads(GEO_FILE.read_text()).get("stamp") == stamp:
                print(f"[geo] gia aggiornato per {stamp}")
                return
        except Exception:
            pass
    url = f"https://download.db-ip.com/free/dbip-country-lite-{stamp}.csv.gz"
    print(f"[geo] scarico {url}")
    # DB-IP risponde 403 allo user-agent di default di urllib: uno esplicito basta.
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "aibridge-collector/1.0 (+https://aibridge.bot.nu)"},
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        raw = gzip.GzipFile(fileobj=io.BytesIO(resp.read())).read().decode("utf-8", "replace")

    v4, v6 = [], []
    for line in raw.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split(",")
        if len(parts) < 3:
            continue
        start_s, end_s, cc = parts[0], parts[1], parts[2]
        try:
            start = ipaddress.ip_address(start_s)
            end = ipaddress.ip_address(end_s)
        except ValueError:
            continue
        if start.version == 4:
            v4.append((int(start), int(end), cc))
        else:
            v6.append((int(start), int(end), cc))

    v4.sort(key=lambda r: r[0])
    v6.sort(key=lambda r: r[0])
    payload = {
        "stamp": stamp,
        "built": now.isoformat(timespec="seconds"),
        "source": "DB-IP Lite (CC BY 4.0) https://db-ip.com",
        "v4_start": [r[0] for r in v4],
        "v6_start": [r[0] for r in v6],
        "v4": [[r[1], r[2]] for r in v4],
        "v6": [[r[1], r[2]] for r in v6],
    }
    GEO_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = GEO_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")))
    tmp.replace(GEO_FILE)
    print(f"[geo] {len(v4)} range IPv4, {len(v6)} range IPv6 -> {GEO_FILE}")


# --------------------------------------------------------------------------- db
SCHEMA = """
CREATE TABLE IF NOT EXISTS hits (
    minute TEXT NOT NULL, grp TEXT NOT NULL, method TEXT NOT NULL,
    status INTEGER NOT NULL, count INTEGER NOT NULL DEFAULT 0,
    bytes INTEGER NOT NULL DEFAULT 0, dur_ms REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (minute, grp, method, status)
);
CREATE TABLE IF NOT EXISTS country_day (
    day TEXT NOT NULL, country TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, country)
);
CREATE TABLE IF NOT EXISTS client_day (
    day TEXT NOT NULL, client TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, client)
);
CREATE TABLE IF NOT EXISTS uniques (
    day TEXT NOT NULL, ip_mask TEXT NOT NULL, hits INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, ip_mask)
);
CREATE TABLE IF NOT EXISTS limited (
    day TEXT NOT NULL, ip_mask TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, ip_mask)
);
CREATE TABLE IF NOT EXISTS kind_day (
    day TEXT NOT NULL, kind TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, kind)
);
CREATE TABLE IF NOT EXISTS auth_day (
    day TEXT NOT NULL, grp TEXT NOT NULL, auth TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, grp, auth)
);
"""


def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE_FILE)


# --------------------------------------------------------------------------- ingest
def ingest(conn: sqlite3.Connection, geo) -> int:
    state = load_state()
    hll_store = load_hll()
    regs_all = hll_regs(hll_store, "all")
    regs_by_kind: dict[str, bytearray] = {}
    files = sorted(LOG_DIR.glob("access*.log"), key=lambda p: p.name)
    total = 0
    for path in files:
        key = path.name
        offset = int(state.get(key, 0))
        size = path.stat().st_size
        if size < offset:
            offset = 0  # file ruotato/troncato
        if size == offset:
            continue
        with path.open("rb") as fh:
            fh.seek(offset)
            chunk = fh.read()
        end = chunk.rfind(b"\n")
        if end < 0:
            continue
        usable = chunk[: end + 1]
        for line in usable.splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("logger") != "http.log.access.log0" and entry.get("msg") != "handled request":
                if "request" not in entry:
                    continue
            req = entry.get("request") or {}
            path_only = (req.get("uri") or "/").split("?", 1)[0]
            grp = group_of(path_only)
            method = (req.get("method") or "?")[:10]
            status = int(entry.get("status") or 0)
            size_b = int(entry.get("size") or 0)
            dur_ms = float(entry.get("duration") or 0) * 1000.0
            ts = float(entry.get("ts") or time.time())
            when = datetime.fromtimestamp(ts, timezone.utc)
            minute = when.strftime("%Y-%m-%dT%H:%M")
            day = when.strftime("%Y-%m-%d")

            conn.execute(
                """INSERT INTO hits(minute, grp, method, status, count, bytes, dur_ms)
                   VALUES(?,?,?,?,1,?,?)
                   ON CONFLICT(minute, grp, method, status)
                   DO UPDATE SET count = count + 1, bytes = bytes + excluded.bytes,
                                 dur_ms = dur_ms + excluded.dur_ms""",
                (minute, grp, method, status, size_b, dur_ms),
            )

            raw_ip = req.get("remote_ip") or ""
            masked, _ver = mask_ip(raw_ip)
            cc = country_of(raw_ip, geo)
            conn.execute(
                """INSERT INTO country_day(day, country, count) VALUES(?,?,1)
                   ON CONFLICT(day, country) DO UPDATE SET count = count + 1""",
                (day, cc),
            )
            # Categoria di autenticazione, dall'header che mette il ponte.
            # Caddy logga gli header di risposta ma NON la chiave: qui arriva solo
            # "key" / "server" / "none" / "denied" / "public".
            auth_raw = (entry.get("resp_headers") or {}).get("X-Polli-Auth")
            if isinstance(auth_raw, list) and auth_raw:
                auth = str(auth_raw[0]).strip().lower()[:12]
            elif isinstance(auth_raw, str) and auth_raw:
                auth = auth_raw.strip().lower()[:12]
            else:
                # nessun header = risposta non marcata (log vecchi, o errore prima
                # del middleware): la contiamo come traffico non-API.
                auth = "static"
            conn.execute(
                """INSERT INTO auth_day(day, grp, auth, count) VALUES(?,?,?,1)
                   ON CONFLICT(day, grp, auth) DO UPDATE SET count = count + 1""",
                (day, grp, auth),
            )

            ua = ((req.get("headers") or {}).get("User-Agent") or [""])[0]
            kind = kind_of(ua)
            conn.execute(
                """INSERT INTO kind_day(day, kind, count) VALUES(?,?,1)
                   ON CONFLICT(day, kind) DO UPDATE SET count = count + 1""",
                (day, kind),
            )
            if masked and masked != "invalid":
                # HLL: una sola chiave per visitatore (IP mascherato), nessun giorno,
                # cosi' il conteggio e' all-time e non sommabile a mano.
                hll_add(regs_all, (hll_store["salt"] + "|" + masked).encode())
                hll_add(regs_by_kind.setdefault(kind, bytearray(HLL_M)),
                        (hll_store["salt"] + "|" + kind + "|" + masked).encode())
            conn.execute(
                """INSERT INTO client_day(day, client, count) VALUES(?,?,1)
                   ON CONFLICT(day, client) DO UPDATE SET count = count + 1""",
                (day, client_family(ua)),
            )
            if masked != "invalid":
                conn.execute(
                    """INSERT INTO uniques(day, ip_mask, hits) VALUES(?,?,1)
                       ON CONFLICT(day, ip_mask) DO UPDATE SET hits = hits + 1""",
                    (day, masked),
                )
                if status == 429:
                    conn.execute(
                        """INSERT INTO limited(day, ip_mask, count) VALUES(?,?,1)
                           ON CONFLICT(day, ip_mask) DO UPDATE SET count = count + 1""",
                        (day, masked),
                    )
            total += 1
        state[key] = offset + len(usable)
        save_state(state)
    conn.commit()

    # salva gli sketch (all + per tipo di client)
    regs_by_kind["all"] = regs_all
    # Si parte da TUTTI gli sketch gia' su disco: sovrascrivere il dizionario con i soli
    # sketch toccati in questo giro cancellerebbe quelli per tipo (bug visto il 23/11).
    merged: dict[str, bytearray] = {name: hll_regs(hll_store, name)
                                    for name in hll_store.get("sketches", {})}
    for name, regs in regs_by_kind.items():
        old = merged.get(name, bytearray(HLL_M))
        for i, r in enumerate(regs):
            if r > old[i]:
                old[i] = r
        merged[name] = old
    save_hll(hll_store, merged)
    return total


def prune(conn: sqlite3.Connection) -> None:
    minute_cut = (datetime.now(timezone.utc) - timedelta(days=MINUTE_RETENTION_DAYS)).strftime("%Y-%m-%dT%H:%M")
    conn.execute("DELETE FROM hits WHERE minute < ?", (minute_cut,))
    day_cut = (datetime.now(timezone.utc) - timedelta(days=UNIQUE_RETENTION_DAYS)).strftime("%Y-%m-%d")
    conn.execute("DELETE FROM uniques WHERE day < ?", (day_cut,))
    conn.execute("DELETE FROM limited WHERE day < ?", (day_cut,))
    conn.commit()
    # log grezzi: oltre la retention si cancellano (i ruotati, mai l'attivo)
    horizon = time.time() - RAW_RETENTION_DAYS * 86400
    for path in LOG_DIR.glob("access-*.log"):
        if path.stat().st_mtime < horizon:
            path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- output
def p95(values: list[float]) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    idx = min(len(values) - 1, int(round(0.95 * (len(values) - 1))))
    return values[idx]


def collect(conn: sqlite3.Connection) -> dict:
    now = datetime.now(timezone.utc)
    day_ago = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M")
    today = now.strftime("%Y-%m-%d")

    def q(sql, args=()):
        return conn.execute(sql, args).fetchall()

    total = q("SELECT COALESCE(SUM(count),0) FROM hits")[0][0]
    total_24h = q("SELECT COALESCE(SUM(count),0) FROM hits WHERE minute >= ?", (day_ago,))[0][0]
    total_7d = q("SELECT COALESCE(SUM(count),0) FROM hits WHERE minute >= ?", (week_ago,))[0][0]
    bytes_7d = q("SELECT COALESCE(SUM(bytes),0) FROM hits WHERE minute >= ?", (week_ago,))[0][0]
    uniques_today = q("SELECT COUNT(*) FROM uniques WHERE day = ?", (today,))[0][0]
    uniques_7d = q("SELECT COUNT(*) FROM uniques WHERE day >= ?",
                   ((now - timedelta(days=7)).strftime("%Y-%m-%d"),))[0][0]
    limited_24h = q("SELECT COALESCE(SUM(count),0) FROM limited WHERE day = ?", (today,))[0][0]

    statuses = dict(q("""SELECT status, SUM(count) FROM hits WHERE minute >= ?
                         GROUP BY status ORDER BY 2 DESC""", (week_ago,)))
    endpoints = q("""SELECT grp, SUM(count), SUM(bytes) FROM hits WHERE minute >= ?
                     GROUP BY grp ORDER BY 2 DESC LIMIT 14""", (week_ago,))
    countries = q("""SELECT country, SUM(count) FROM country_day WHERE day >= ?
                     GROUP BY country ORDER BY 2 DESC LIMIT 12""",
                  ((now - timedelta(days=7)).strftime("%Y-%m-%d"),))
    clients = q("""SELECT client, SUM(count) FROM client_day WHERE day >= ?
                   GROUP BY client ORDER BY 2 DESC LIMIT 10""",
                ((now - timedelta(days=7)).strftime("%Y-%m-%d"),))
    week_day = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    auth_7d = q("""SELECT auth, SUM(count) FROM auth_day WHERE day >= ?
                   GROUP BY auth ORDER BY 2 DESC""", (week_day,))
    auth_by_group = q("""SELECT grp, auth, SUM(count) FROM auth_day WHERE day >= ?
                         GROUP BY grp, auth ORDER BY 3 DESC LIMIT 40""", (week_day,))
    keyed_24h = q("""SELECT COALESCE(SUM(count),0) FROM auth_day
                     WHERE day = ? AND auth IN ('key','server')""", (today,))[0][0]
    anon_24h = q("""SELECT COALESCE(SUM(count),0) FROM auth_day
                    WHERE day = ? AND auth IN ('none','denied')""", (today,))[0][0]
    static_24h = q("""SELECT COALESCE(SUM(count),0) FROM auth_day
                      WHERE day = ? AND auth IN ('static','public')""", (today,))[0][0]

    avg_dur = q("""SELECT grp, SUM(dur_ms)/SUM(count) FROM hits WHERE minute >= ?
                   GROUP BY grp HAVING SUM(count) > 0 ORDER BY 2 DESC LIMIT 8""", (week_ago,))

    hourly = q("""SELECT substr(minute,1,13) AS h, SUM(count) FROM hits
                  WHERE minute >= ? GROUP BY h ORDER BY h""",
               ((now - timedelta(hours=24)).strftime("%Y-%m-%dT%H"),))

    daily = q("""SELECT substr(minute,1,10) AS d, SUM(count), SUM(bytes) FROM hits
                 WHERE minute >= ? GROUP BY d ORDER BY d""",
              ((now - timedelta(days=14)).strftime("%Y-%m-%d"),))

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "generated_at_local": now.astimezone(LOCAL_TZ).isoformat(timespec="seconds"),
        "totals": {
            "requests_all_time": total,
            "requests_24h": total_24h,
            "requests_7d": total_7d,
            "bytes_7d": bytes_7d,
            "unique_visitors_24h": uniques_today,
            "unique_visitors_7d": uniques_7d,
            "rate_limited_24h": limited_24h,
            "keyed_24h": keyed_24h,
            "anonymous_24h": anon_24h,
            "static_24h": static_24h,
        },
        "auth_7d": [{"auth": a, "requests": n} for a, n in auth_7d],
        "auth_by_group_7d": [{"path": g, "auth": a, "requests": n} for g, a, n in auth_by_group],
        "status_7d": {str(k): v for k, v in statuses.items()},
        "endpoints_7d": [{"path": g, "requests": c, "bytes": b} for g, c, b in endpoints],
        "countries_7d": [{"country": c, "requests": n} for c, n in countries],
        "clients_7d": [{"client": c, "requests": n} for c, n in clients],
        "avg_ms_by_endpoint_7d": [{"path": g, "avg_ms": round(v, 1)} for g, v in avg_dur],
        "hourly_24h": [{"hour": h, "requests": n} for h, n in hourly],
        "daily_14d": [{"day": d, "requests": n, "bytes": b} for d, n, b in daily],
        "privacy": {
            "ip_storage": "masked only (/24 IPv4, /64 IPv6), aggregates, 7 days",
            "not_stored": ["prompts", "completions", "headers", "api keys", "cookies", "user agents (full)"],
            "geo_source": "DB-IP Lite (CC BY 4.0), offline lookup",
        },
    }


def sparkline(values: list[int], width: int = 640, height: int = 90) -> str:
    if not values:
        return ""
    top = max(values) or 1
    step = width / max(len(values) - 1, 1)
    pts = " ".join(f"{i * step:.1f},{height - (v / top) * (height - 8) - 4:.1f}" for i, v in enumerate(values))
    return (f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
            f'preserveAspectRatio="none" role="img" aria-label="richieste per ora">'
            f'<polyline points="{pts}" fill="none" stroke="#7ee787" stroke-width="2"/>'
            f'<polygon points="0,{height} {pts} {width},{height}" fill="#7ee787" opacity="0.12"/></svg>')


def rows(items, cols) -> str:
    out = []
    for item in items:
        tds = "".join(f"<td>{col(item)}</td>" for col in cols)
        out.append(f"<tr>{tds}</tr>")
    return "".join(out) or '<tr><td colspan="9" class="dim">nessun dato ancora</td></tr>'


def esc(value) -> str:
    return (str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def render_html(data: dict) -> str:
    t = data["totals"]
    hours = [h["requests"] for h in data["hourly_24h"]]
    spark = sparkline(hours)

    def bytes_h(n):
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024:
                return f"{n:.0f} {unit}"
            n /= 1024
        return f"{n:.1f} TB"

    status_rows = rows(
        [{"s": k, "n": v} for k, v in data["status_7d"].items()],
        [lambda r: esc(r["s"]), lambda r: f"{r['n']:,}"],
    )
    endpoint_rows = rows(
        data["endpoints_7d"],
        [lambda r: f"<code>{esc(r['path'])}</code>", lambda r: f"{r['requests']:,}", lambda r: bytes_h(r["bytes"])],
    )
    country_rows = rows(
        data["countries_7d"],
        [lambda r: esc(r["country"]), lambda r: f"{r['requests']:,}"],
    )
    client_rows = rows(
        data["clients_7d"],
        [lambda r: esc(r["client"]), lambda r: f"{r['requests']:,}"],
    )
    auth_labels = {
        "key": "con chiave",
        "server": "chiave dell'istanza",
        "none": "senza chiave (modalita' aperta)",
        "denied": "rifiutata (401)",
        "public": "endpoint pubblico",
        "static": "statico / crawler",
    }
    auth_rows = rows(
        [{"a": auth_labels.get(a["auth"], a["auth"]), "n": a["requests"]} for a in data.get("auth_7d", [])],
        [lambda r: esc(r["a"]), lambda r: f"{r['n']:,}"],
    )
    auth_group_rows = rows(
        data.get("auth_by_group_7d", []),
        [lambda r: f"<code>{esc(r['path'])}</code>", lambda r: esc(r["auth"]), lambda r: f"{r['requests']:,}"],
    )
    latency_rows = rows(
        data["avg_ms_by_endpoint_7d"],
        [lambda r: f"<code>{esc(r['path'])}</code>", lambda r: f"{r['avg_ms']} ms"],
    )

    return f"""<!doctype html>
<html lang="it"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>aibridge — statistiche di traffico</title>
<meta name="robots" content="noindex">
<style>
:root{{--bg:#0b0d10;--panel:#14181d;--line:#242c35;--fg:#e7edf3;--dim:#9aa8b6;--acc:#7ee787;--acc2:#79c0ff}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}}
a{{color:var(--acc2);text-decoration:none}}a:hover{{text-decoration:underline}}
code{{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:13px}}
.wrap{{max-width:960px;margin:0 auto;padding:28px 20px 70px}}
h1{{font-size:24px;margin:0 0 4px}}h2{{font-size:16px;margin:30px 0 8px;padding-top:12px;border-top:1px solid var(--line);color:var(--dim);text-transform:uppercase;letter-spacing:.06em}}
.dim{{color:var(--dim)}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:18px 0}}
.card{{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px}}
.card .n{{font-size:26px;font-variant-numeric:tabular-nums}}
.card .l{{font-size:12px;color:var(--dim);text-transform:uppercase;letter-spacing:.05em}}
table{{width:100%;border-collapse:collapse;font-size:14px;margin:8px 0}}
th,td{{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line)}}
th{{color:var(--dim);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.05em}}
td:not(:first-child),th:not(:first-child){{text-align:right;font-variant-numeric:tabular-nums}}
.panel{{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:8px 14px;margin:10px 0}}
footer{{margin-top:34px;color:var(--dim);font-size:13px}}
</style></head><body><div class="wrap">
<h1>aibridge — statistiche di traffico</h1>
<p class="dim">Aggregati, niente dati personali. IP mascherati <code>/24</code> e <code>/64</code>, log grezzi cancellati dopo 7 giorni.
Generato: <strong>{esc(data['generated_at_local'])}</strong> · <a href="/stats.json">stats.json</a> · <a href="/">torna alla landing</a></p>

<div class="cards">
  <div class="card"><div class="n">{t['requests_24h']:,}</div><div class="l">richieste 24h</div></div>
  <div class="card"><div class="n">{t['requests_7d']:,}</div><div class="l">richieste 7 giorni</div></div>
  <div class="card"><div class="n">{t['requests_all_time']:,}</div><div class="l">totale</div></div>
  <div class="card"><div class="n">{t['unique_visitors_24h']:,}</div><div class="l">visitatori unici 24h</div></div>
  <div class="card"><div class="n">{t['unique_visitors_7d']:,}</div><div class="l">unici 7 giorni</div></div>
  <div class="card"><div class="n">{bytes_h(t['bytes_7d'])}</div><div class="l">byte serviti 7g</div></div>
  <div class="card"><div class="n">{t.get('keyed_24h', 0):,}</div><div class="l">con chiave 24h</div></div>
  <div class="card"><div class="n">{t.get('anonymous_24h', 0):,}</div><div class="l">senza chiave 24h</div></div>
  <div class="card"><div class="n">{t.get('static_24h', 0):,}</div><div class="l">statico / crawler 24h</div></div>
  <div class="card"><div class="n">{t['rate_limited_24h']:,}</div><div class="l">429 oggi</div></div>
</div>

<h2>Ultime 24 ore</h2>
<div class="panel">{spark or '<p class="dim">nessun dato</p>'}</div>

<h2>Endpoint (7 giorni)</h2>
<div class="panel"><table><tr><th>endpoint</th><th>richieste</th><th>byte</th></tr>{endpoint_rows}</table></div>

<h2>Autenticazione (7 giorni)</h2>
<div class="panel"><table><tr><th>come ha chiamato</th><th>richieste</th></tr>{auth_rows}</table></div>
<div class="panel"><table><tr><th>endpoint</th><th>auth</th><th>richieste</th></tr>{auth_group_rows}</table></div>

<h2>Stato HTTP (7 giorni)</h2>
<div class="panel"><table><tr><th>status</th><th>richieste</th></tr>{status_rows}</table></div>

<h2>Paesi (7 giorni)</h2>
<div class="panel"><table><tr><th>paese</th><th>richieste</th></tr>{country_rows}</table></div>

<h2>Client (7 giorni, solo famiglia)</h2>
<div class="panel"><table><tr><th>client</th><th>richieste</th></tr>{client_rows}</table></div>

<h2>Latenza media</h2>
<div class="panel"><table><tr><th>endpoint</th><th>media</th></tr>{latency_rows}</table></div>

<footer>
<p>Cosa viene registrato: IP mascherato, paese (DB-IP Lite, offline), timestamp, path ridotto a un gruppo fisso,
status, latenza, byte, famiglia di user agent, e se la chiamata aveva una chiave (categoria, <strong>mai</strong> il valore). <strong>Non</strong> vengono registrati prompt, risposte, header grezzi, chiavi, cookie.
Nessun identificatore persistente, nessun profiling, nessuna vendita di dati.</p>
<p><a href="https://github.com/BalTac/polli-bridge">polli-bridge</a> · <a href="https://pollinations.ai/?ref=badge">pollinations.ai</a></p>
</footer>
</div></body></html>
"""


def main(argv: list[str]) -> int:
    # --geo     : aggiorna la tabella paesi e basta
    # --with-geo: aggiorna la tabella paesi (se serve) e poi fa il giro normale
    want_geo = "--geo" in argv or "--with-geo" in argv
    if want_geo:
        build_geo(force="--force" in argv)
    if "--geo" in argv and "--with-geo" not in argv:
        return 0
    geo = load_geo()
    conn = db()
    if KINDS_FLAG.exists():
        lines, groups = backfill_kinds(conn, geo)
        try:
            KINDS_FLAG.unlink()
        except OSError:
            pass
        print(f"[collector] kind_day ricostruita da {lines} righe di log ({groups} gruppi giorno/tipo)")
    if SEED_FLAG.exists():
        store = load_hll()
        seeded = seed_hll_from_uniques(conn, store)
        try:
            SEED_FLAG.unlink()
        except OSError:
            pass
        print(f"[collector] HLL seminato da {seeded} IP mascherati storici")
    processed = ingest(conn, geo)
    prune(conn)
    data = collect(conn)
    # va letto PRIMA di chiudere la connessione: conn.execute su un db chiuso solleva
    kinds_today = {k: n for k, n in conn.execute(
        "SELECT kind, SUM(count) FROM kind_day WHERE day = ? GROUP BY kind ORDER BY 2 DESC",
        (datetime.now(timezone.utc).strftime("%Y-%m-%d"),)).fetchall()}
    conn.close()
    OUT_JSON.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    OUT_HTML.write_text(render_html(data))

    # Indicatori interni: NON pubblicati (stanno in data/, Caddy serve solo site/).
    # Visitatori unici all-time stimati con HLL, spezzati per tipo di client.
    store = load_hll()
    kinds = {name: hll_count(hll_regs(store, name)) for name in list(store.get("sketches", {}))}
    indicators = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "unique_all_time_hll": kinds.get("all", 0),
        "unique_by_kind_hll": kinds,
        "hll": {"registers": HLL_M, "error_pct": 0.81, "salt_present": bool(store.get("salt"))},
        "kinds_today": kinds_today,
    }
    INDICATORS.write_text(json.dumps(indicators, indent=1, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(INDICATORS, 0o644)
    except OSError:
        pass

    print(f"[collector] {processed} righe nuove · {data['totals']['requests_24h']} richieste/24h · "
          f"{data['totals']['unique_visitors_24h']} unici/24h -> {OUT_HTML.name}")
    print(f"  unici all-time (HLL, non pubblicato): {indicators['unique_all_time_hll']} · "
          f"{', '.join(f'{k} {v}' for k, v in kinds.items() if k != 'all')}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
