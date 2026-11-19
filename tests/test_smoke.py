"""Smoke test minimi — solo stdlib, nessuna rete, nessuna spesa.

Chiamano l'app ASGI in-process. Il test 429 esiste per impedire il ritorno
del bug storico: HTTPException risputata come 500.

Esecuzione:  venv/bin/python -m unittest discover -s tests -v
"""
import asyncio
import json
import os
import tempfile
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server
from fastapi import HTTPException
from client import PollinationsClient

# I test girano sempre senza auth, qualunque cosa ci sia nel .env locale.
server.LOCAL_API_TOKEN = None


def call(method, path, query="", extra_headers=None, body=b""):
    """Chiama server.app via ASGI. Ritorna (status, body_bytes)."""
    result = {}
    sent_body = bytearray()
    messages = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            result["status"] = message["status"]
            result["headers"] = {k.decode(): v.decode() for k, v in message.get("headers", [])}
        elif message["type"] == "http.response.body":
            sent_body.extend(message.get("body", b""))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": [(b"host", b"testserver")] + list(extra_headers or []),
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8083),
    }
    asyncio.run(server.app(scope, receive, send))
    return result.get("status"), bytes(sent_body), result.get("headers", {})


class SmokeTests(unittest.TestCase):
    def test_health_is_ok(self):
        status, body, _ = call("GET", "/health")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["service"], "pollinations-api-server")

    def test_swagger_redirects_to_docs(self):
        status, _, headers = call("GET", "/swagger")
        self.assertEqual(status, 307)
        self.assertEqual(headers.get("location"), "/docs")

    def test_redoc_and_docs_available(self):
        for path in ("/docs", "/redoc"):
            status, _, _ = call("GET", path)
            self.assertEqual(status, 200, path)

    def test_generate_requires_prompt(self):
        status, _, _ = call("GET", "/generate")
        self.assertEqual(status, 422)

    def test_upstream_429_survives_the_handler(self):
        """Regressione B1: un 429 upstream deve arrivare come 429, non 500."""
        async def boom(*args, **kwargs):
            raise HTTPException(status_code=429, detail="rate limit upstream")

        original = server.client.generate_image
        server.client.generate_image = boom
        try:
            status, body, _ = call("GET", "/generate", "prompt=un%20gatto")
        finally:
            server.client.generate_image = original
        self.assertEqual(status, 429, body[:200])

    def test_split_text_respects_limit(self):
        client = PollinationsClient(image_api_url="http://stub", text_api_url="http://stub")
        text = "parola " * 500
        chunks = client._split_text(text, 750)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 750 for c in chunks), max(len(c) for c in chunks))
        self.assertTrue(chunks[0].startswith("parola"))
        self.assertTrue(chunks[-1].strip().endswith("parola"))


class LocalAuthTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(server, "LOCAL_API_TOKEN"))
        self._saved = server.LOCAL_API_TOKEN

    def tearDown(self):
        server.LOCAL_API_TOKEN = self._saved

    def test_auth_off_by_default(self):
        # path inesistente: 404 (non 401) significa che il middleware non blocca.
        # Niente prompt reali nei test: genererebbero traffico fatturato.
        server.LOCAL_API_TOKEN = None
        status, _, _ = call("GET", "/percorso-inesistente")
        self.assertEqual(status, 404)

    def test_auth_on_rejects_without_key(self):
        server.LOCAL_API_TOKEN = "segreto"
        status, _, _ = call("GET", "/percorso-inesistente")
        self.assertEqual(status, 401)

    def test_auth_on_accepts_bearer_and_x_api_key(self):
        server.LOCAL_API_TOKEN = "segreto"
        header_bearer = [(b"authorization", b"Bearer segreto")]
        header_key = [(b"x-api-key", b"segreto")]
        status_bearer, _, _ = call("GET", "/percorso-inesistente", "", header_bearer)
        status_key, _, _ = call("GET", "/percorso-inesistente", "", header_key)
        self.assertEqual(status_bearer, 404)  # passa l auth, poi 404 di routing
        self.assertEqual(status_key, 404)

    def test_auth_on_keeps_health_open(self):
        server.LOCAL_API_TOKEN = "segreto"
        status, _, _ = call("GET", "/health")
        self.assertEqual(status, 200)


class OpenAITests(unittest.TestCase):
    def test_models_list_is_openai_shaped(self):
        status, body, _ = call("GET", "/v1/models")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["object"], "list")
        self.assertTrue(payload["data"])
        first = payload["data"][0]
        self.assertIn("id", first)
        self.assertIn("category", first)

    def test_chat_completions_proxies_json(self):
        async def fake_proxy(method, path, payload=None, stream=False):
            assert path == "/v1/chat/completions"
            return 200, {"id": "chatcmpl-1", "choices": [{"message": {"content": "ok"}}]}, "application/json", None

        original = server.client.proxy_json
        server.client.proxy_json = fake_proxy
        try:
            status, body, _ = call("POST", "/v1/chat/completions", "",
                                   [(b"content-type", b"application/json")],
                                   b'{"model":"m","messages":[]}')
        finally:
            server.client.proxy_json = original
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["id"], "chatcmpl-1")

    def test_images_generations_error_passes_through(self):
        async def failing_proxy(method, path, payload=None, stream=False):
            return 429, None, "application/json", "rate limit upstream"

        original = server.client.proxy_json
        server.client.proxy_json = failing_proxy
        try:
            status, _, _ = call("POST", "/v1/images/generations", "",
                                [(b"content-type", b"application/json")], b'{"prompt":"x"}')
        finally:
            server.client.proxy_json = original
        self.assertEqual(status, 429)


class MediaTests(unittest.TestCase):
    def _mock(self, name, value):
        original = getattr(server.client, name)
        setattr(server.client, name, value)
        return original

    def test_video_returns_binary(self):
        async def fake_get(path, params=None, timeout=None):
            self.assertEqual(path, "/video/un%20tramonto")
            self.assertIn("model", params)
            return 200, b"\x00\x00\x00\x18ftypmp42", "video/mp4", None

        original = self._mock("proxy_get", fake_get)
        try:
            status, body, headers = call("GET", "/video/un tramonto", "model=google/veo-3.1-fast")
        finally:
            setattr(server.client, "proxy_get", original)
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"\x00\x00\x00\x18ftyp"))

    def test_job_status_is_json(self):
        async def fake_get(path, params=None, timeout=None):
            self.assertEqual(path, "/v1/jobs/abc123")
            return 200, {"id": "abc123", "status": "running"}, "application/json", None

        original = self._mock("proxy_get", fake_get)
        try:
            status, body, _ = call("GET", "/v1/jobs/abc123")
        finally:
            setattr(server.client, "proxy_get", original)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "running")

    def test_media_upstream_error_passes_through(self):
        async def fake_get(path, params=None, timeout=None):
            return 402, None, "application/json", "payment required"

        original = self._mock("proxy_get", fake_get)
        try:
            status, _, _ = call("GET", "/3d/una%20sedia")
        finally:
            setattr(server.client, "proxy_get", original)
        self.assertEqual(status, 402)


class TimestampTests(unittest.TestCase):
    def test_with_timestamps_proxies(self):
        async def fake_proxy(method, path, payload=None, stream=False):
            self.assertEqual(path, "/v1/audio/speech/with-timestamps")
            return 200, {"audio": "base64", "timestamps": []}, "application/json", None

        original = server.client.proxy_json
        server.client.proxy_json = fake_proxy
        try:
            status, body, _ = call("POST", "/v1/audio/speech/with-timestamps", "",
                                   [(b"content-type", b"application/json")], b'{"input":"ciao"}')
        finally:
            server.client.proxy_json = original
        self.assertEqual(status, 200)
        self.assertIn("timestamps", json.loads(body))


class TranscriptionTests(unittest.TestCase):
    """La trascrizione deve passare dall'endpoint dedicato, non da chat+input_audio."""

    def _run(self, upstream_payload, monkeypatch_path=None):
        client_obj = server.client
        calls = {}

        async def fake_preprocess(path):
            return path

        async def fake_multipart(path, fields, files):
            calls['path'] = path
            calls['fields'] = dict(fields)
            calls['files'] = list((files or {}).keys())
            return 200, upstream_payload, "application/json", None

        original_mp = client_obj.proxy_multipart
        original_pp = client_obj._preprocess_audio
        client_obj.proxy_multipart = fake_multipart
        client_obj._preprocess_audio = fake_preprocess
        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tmp.write(b"ID3fake-frame")
        tmp.close()
        try:
            status, payload, error = asyncio.run(client_obj.transcribe_audio_file_stt(
                audio_file_path=tmp.name, original_filename="prova.mp3", language="it"))
        finally:
            client_obj.proxy_multipart = original_mp
            client_obj._preprocess_audio = original_pp
            if os.path.exists(tmp.name):
                os.unlink(tmp.name)
        return status, payload, error, calls

    def test_uses_dedicated_transcription_endpoint(self):
        status, payload, error, calls = self._run({"text": "ciao mondo"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["transcription"], "ciao mondo")
        self.assertEqual(calls["path"], "/v1/audio/transcriptions")
        self.assertEqual(calls["files"], ["file"])
        self.assertEqual(calls["fields"]["language"], "it")

    def test_empty_transcription_is_an_upstream_error_not_a_crash(self):
        status, payload, error, _ = self._run({"text": ""})
        self.assertEqual(status, 502)
        self.assertIsNone(payload)
        self.assertIn("no text", error)


class TtsTests(unittest.TestCase):
    """Il TTS deve leggere il testo e basta, sull'endpoint dedicato."""

    def test_tts_uses_speech_endpoint_with_plain_text(self):
        client_obj = server.client
        seen = {}

        async def fake_request(method, url, params=None, headers=None, json_data=None, stream=False, timeout=None):
            seen['url'] = url
            seen['payload'] = json_data
            return 200, b"ID3fake-mp3", "audio/mpeg", None

        original = client_obj._make_request
        client_obj._make_request = fake_request
        try:
            status, data, ctype, error = asyncio.run(client_obj._make_tts_api_call(
                "Ciao mondo", "nova", "openai-audio", "mp3", False, "it"))
        finally:
            client_obj._make_request = original

        self.assertEqual(status, 200)
        self.assertEqual(data, b"ID3fake-mp3")
        self.assertTrue(seen['url'].endswith("/v1/audio/speech"), seen['url'])
        self.assertEqual(seen['payload']['input'], "Ciao mondo")
        self.assertEqual(seen['payload']['model'], "openai/tts-1")

    def test_tts_prompt_has_no_instruction_preamble(self):
        prompt = server.client._prepare_tts_prompt("  ciao mondo  ", "it")
        self.assertEqual(prompt, "ciao mondo")
        self.assertNotIn("REPEAT", prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
