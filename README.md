[![Made with pollinations.ai](https://raw.githubusercontent.com/pollinations/pollinations/main/packages/ui/src/brand/badge-made-with.svg)](https://pollinations.ai/?ref=badge)

# polli-bridge

**One OpenAI-compatible `/v1` endpoint in front of [Pollinations](https://pollinations.ai).**
Text, images, image edits, speech, transcription, video, 3D and embeddings — every client that
speaks the OpenAI API works by changing a single `base_url`. Self-hosted, one secret key, no
database, no account system.

**Live demo — bring your own key: <https://aibridge.bot.nu>**
That deployment holds no key of its own: it forwards your key, bills your balance, and
returns 401 without one. How it is built (TLS, rate limit, privacy-preserving stats):
[DEPLOY.md](DEPLOY.md).


![Swagger UI, all endpoints](docs/screenshot.png)


```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8083/v1", api_key="not-used")
client.chat.completions.create(
    model="openai/gpt-5.4-nano",
    messages=[{"role": "user", "content": "Perché il cielo è blu?"}],
)
```

## Why

Pollinations already exposes an OpenAI-compatible surface, and its official docs are the source of
truth: **<https://gen.pollinations.ai/docs>**. This project exists for the cases where you want:

- **one key in one place** — a homelab, a NAS or a single box holds the secret; every tool
  (OpenClaw, Continue, Cline, LangChain, LiteLLM, Open WebUI, `openai-python`) just points at it;
- **one base URL for every modality**, including the native-only parts (`/video/{prompt}`,
  `/3d/{prompt}`, `/v1/jobs/*`) that OpenAI-shaped clients cannot reach;
- **the local media plumbing** that a thin proxy does not give you: long-text TTS split into
  chunks and merged back with ffmpeg, long-audio transcription split into segments, prompt
  enhancement, a cached model catalog, upstream error codes preserved (a 429 stays a 429).

It is a bridge, not a platform: no users, no billing, no admin panel, no fan-out to other
providers. If you need a multi-provider gateway or an image-only shim, those exist too — see
[Prior art](#prior-art).

## Quick start

```bash
git clone https://github.com/BalTac/polli-bridge && cd polli-bridge
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then set POLLINATIONS_API_TOKEN
python server.py              # http://127.0.0.1:8083/docs
```

Docker:

```bash
docker build -t polli-bridge .
docker run --rm -p 127.0.0.1:8083:8083 --env-file .env polli-bridge
# or: docker compose up -d
```

Point any OpenAI client at `http://<host>:8083/v1`. The `api_key` you pass is ignored; the
upstream key comes from `.env`.

## Endpoints

**OpenAI-compatible**

| Method | Path | Notes |
|---|---|---|
| POST | `/v1/chat/completions` | streaming supported |
| POST | `/v1/images/generations` | |
| POST | `/v1/images/edits` | multipart: `image` + `prompt` (+ optional `model`, `size`) |
| POST | `/v1/audio/speech` | returns audio bytes |
| POST | `/v1/audio/speech/with-timestamps` | for subtitles |
| POST | `/v1/audio/transcriptions` | multipart: `file` (+ `language`, `prompt`) |
| POST | `/v1/embeddings` | |
| GET | `/v1/models` | unified catalog with `category`, `pricing`, `paid_only`; 24h cache |

**Image editing needs an image-input model.** The default for `/v1/images/edits` is
`black-forest-labs/flux.2-klein-4b` (free tier). Free alternatives:
`black-forest-labs/flux.1-kontext-pro`, `black-forest-labs/flux.2-flex`,
`microsoft/mai-image-2.6`. Some editing models are paid (`paid_only: true` in the catalog,
e.g. `prunaai/p-image-edit`, `qwen/qwen-image-2.1`, `google/gemini-3.1-flash-image`): they
work only if your account has credits. Check `paid_only` in `/v1/models` before choosing one.

## Decisions (system one)

Pollinations exposes a `decision` model category: instead of free text, you get calibrated
answers — a pick from a list, a score, or the probability of "yes". Useful for routing,
moderation triage and cheap classification that should not spend a text model.

```bash
curl -s http://127.0.0.1:8083/alpha/decisions\
  -H "Authorization: Bearer $POLLINATIONS_API_KEY"\
  -H "Content-Type: application/json"\
  -d '{
    "state": "My payouts have been failing for 3 days.",
    "questions": {
      "department": {
        "type": "choice",
        "instructions": "Which team should handle this?",
        "criteria": { "billing": "Payment issues", "technical": "Product failures" }
      },
      "is_urgent": { "type": "noul", "instructions": "Does this convey urgency?" }
    }
  }'
```

```json
{"id": "dec-…", "model": "microsoft/microsoft-decision-1.1", "provider": "Microsoft",
 "answers": {"department": {"type": "choice", "choice": "billing", "confidence": 0.60,
                            "probabilities": {"billing": 0.80, "technical": 0.20}},
             "is_urgent": {"type": "noul", "noul": 0.99}},
 "usage": {"input_tokens": 66, "output_tokens": 2}}
```

Each question needs `type` (`choice`, `score` or `noul`) and `instructions`; `state`,
`instructions` and `criteria` values accept a string or arbitrary JSON. The request is
validated locally, so a malformed question fails with a 400 that names the field instead of
a generic upstream error. There is no streaming.

**The default is `microsoft/microsoft-decision-1.1` because it is free** (`paid_only: false`).
Upstream defaults to `jev`, which is paid — a caller without credits would fail on the
default path. Other free decision models: `microsoft/microsoft-decision-1`,
`respan/span-01-lite` (yes/no behaviour checks) and `vllm-sr/d3`
(**logs inputs and may use them for training**: fine for a lab, think twice in production).
Paid ones (`typesafe/jev-1.14`, `liquid/d1`, `liquid/d1.2`, `jaredpalmer/kev-4b`,
`perplexity/pplx-decider-v1.2`, `openai/gpt-6-luna-decisions`, `google/gemini-decision-1`)
work only with credits. `GET /models/decision` lists them with `paid_only`.

**Native passthrough** (for parameters OpenAI clients cannot express)

| Method | Path | Notes |
|---|---|---|
| GET | `/video/{prompt}` | MP4, long timeout; `/video/models` |
| GET | `/3d/{prompt}` | GLB; `/3d/models` |
| GET | `/v1/jobs/{id}` · `/v1/jobs/{id}/result` | for upstream-queued work |
| GET | `/video/models` · `/3d/models` · `/embeddings/models` | catalogs |
| POST | `/alpha/decisions` | typed decisions: `choice`, `score`, `noul` |
| POST | `/v1/systemone` | alias of `/alpha/decisions` (TypeSafe-compatible) |
| GET | `/models/decision` | decision models (11 today, 4 free) |

**Legacy routes** kept for compatibility: `/generate`, `/text/generate_get`,
`/text/generate_post`, `/audio/tts_get`, `/audio/tts_post`, `/audio/stt_file_upload`,
`/models/image`, `/models/text`, `/account/balance`, `/health/transcription`.

Interactive docs: `/docs` (Swagger UI), `/redoc` (ReDoc), `/swagger` (alias of `/docs`),
`/health`.

## Security

Two modes, decided by one thing: **whether the server holds a key**.

| `POLLINATIONS_API_TOKEN` | Bind | Mode |
|---|---|---|
| set | loopback | **server key** — the gateway pays with your key |
| set | non-loopback | **server key + `LOCAL_API_TOKEN`** — required, the process refuses to start without it |
| empty | loopback | **open** — you can try it immediately; anonymous requests go out, a caller key is used when present |
| empty | non-loopback | **BYOP** — every caller must send its own key, missing key → `401` |

- Binds to **127.0.0.1** by default.
- With a server key on a non-loopback bind, **`LOCAL_API_TOKEN` is required** — the process refuses
  to start without it. Set it and send `X-API-Key: <token>` or `Authorization: Bearer <token>`.
  `/health` and the docs stay open.
- The upstream key lives only in `.env` (0600) or the container environment. It is never logged
  and never echoed in responses.
- Prompts are **not** written to logs (only upstream status codes and byte counts).
- No telemetry, no database, no state on disk except `.env` and the model catalog cache in
  `models_data/` (24h TTL, safe to delete).

## Serving it publicly (bring your own key)

Leave `POLLINATIONS_API_TOKEN` empty, bind to a public interface, and the same binary becomes a
**BYOP gateway**: your balance is never touched, because there is no server key to spend. Every
request must carry the caller's own key:

```bash
curl http://your-host:8083/v1/chat/completions \
  -H "Authorization: Bearer sk_the_callers_own_key" \
  -H "Content-Type: application/json" \
  -d '{"model": "openai", "messages": [{"role": "user", "content": "hello"}]}'
```

No key → `401` with an explicit message. `/health` reports `"auth": "byop"`.

What to put in front of it if strangers will use it:

- a per-IP rate limit (`limit_req` in nginx, or Caddy `rate_limit`) — the VPS, not your balance,
  is the thing that can be hammered;
- request and response size caps (videos are megabytes);
- TLS (Caddy gets a certificate in two lines);
- logs without headers: keys must never reach a log file.

If you want the traffic attributed to a Pollinations **App Key** (developer earnings and referrals),
users have to obtain their key through your app, i.e. the `<polli-connect>` flow in
[`BRING_YOUR_OWN_POLLEN.md`](https://github.com/pollinations/pollinations/blob/main/BRING_YOUR_OWN_POLLEN.md).
A key copied by hand from the dashboard works, but it is not attributed to anyone.

## Costs

You pay Pollinations, per request, from the key's balance. Text is fractions of a cent; images
are per-image; **video and 3D are billed per second of output** and are the expensive ones — ask
the catalog before generating:

```bash
curl -s "http://127.0.0.1:8083/v1/models" | python -c "import json,sys; [print(m['id'], m.get('pricing')) for m in json.load(sys.stdin)['data'] if m['category']=='video']"
```

Long text sent to `/v1/audio/speech` is split into several requests (that is what makes it work)
so the cost is the sum of the chunks; `TTS_CHUNK_MAX_CHARS` and `POLLINATIONS_TIER` control it.

## Tests

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

29 tests, standard library only, **no network**: the ASGI app is called in-process and the
upstream client is mocked. That is deliberate — a test suite that spends your balance is a test
suite nobody runs.

## Prior art

Honest list, because picking the right tool matters more than picking ours:

- [`pollinations2openai`](https://github.com/pollinations/pollinations/issues/8218) — image-only
  `/v1/images` shim for OpenWebUI/LiteLLM.
- [`AIOPE Gateway`](https://github.com/XNet-NGO/aiope-gateway) — multi-provider LLM proxy with a
  web admin, Pollinations as one upstream.
- [`AngeVox/AngeMedia-gateway`](https://github.com/AngeVox/AngeMedia-gateway) — OpenAI-compatible
  media gateway, multiple providers.
- [`samucamg/veroroute-edge`](https://github.com/samucamg/veroroute-edge) — Cloudflare Workers
  gateway, zero weight.

This project is deliberately the opposite trade-off: **one upstream, the whole surface, no
admin, no state**. Small enough to audit in an afternoon.

## License

MIT — see [LICENSE](LICENSE). Not affiliated with Pollinations; it is an independent client of
their public API.

## Keywords

`pollinations` · `openai-compatible` · `openai-api` · `llm-gateway` · `self-hosted` · `homelab` ·
`fastapi` · `text-to-speech` · `speech-to-text` · `image-generation` · `video-generation` ·
`embeddings` · `docker` · `local-first`
