"""Test del TTS con stile (instructions), scelta del modello e ripiego wav->mp3.

Tre cose che valgono un test perché sono facili da rompere in silenzio:

1. `instructions` e `model` devono arrivare dal bordo fino al client: prima erano
   hardcoded o assenti, quindi una regressione non produce errori, produce voci
   senza stile.
2. Alcuni modelli (es. google/gemini-*-tts) accettano solo wav: il bridge ritenta
   in wav e converte in mp3, così il chiamante riceve sempre mp3.
3. Se la conversione fallisce si restituisce l'audio originale, non un errore.

Niente rete: client e converter sono sostituiti da finti.

Esecuzione:  venv/bin/python -m unittest discover -s tests -v
"""
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server
from client import PollinationsClient

server.LOCAL_API_TOKEN = None


def call(method, path, query="", body=b""):
    result = {}
    sent = bytearray()
    messages = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            result["status"] = message["status"]
        elif message["type"] == "http.response.body":
            sent.extend(message.get("body", b""))

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
        "headers": [(b"host", b"testserver"), (b"content-type", b"application/json")],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8083),
    }
    asyncio.run(server.app(scope, receive, send))
    return result.get("status"), bytes(sent)


class TtsStyleTests(unittest.TestCase):
    """Il bordo deve inoltrare modello e instructions senza taroccarli."""

    def setUp(self):
        self.kwargs = {}
        self._real = server.client.generate_audio_tts_post

        async def fake_tts(**kwargs):
            self.kwargs = kwargs
            return 200, b"ID3fake", "audio/mpeg", None

        server.client.generate_audio_tts_post = fake_tts

    def tearDown(self):
        server.client.generate_audio_tts_post = self._real

    def test_get_forwards_model_and_instructions(self):
        status, _ = call(
            "GET", "/audio/tts_get",
            query="text=ciao&voice=alloy&model=google/gemini-3.8-flash-tts&instructions=voce+sussurrata")
        self.assertEqual(status, 200)
        self.assertEqual(self.kwargs["model"], "google/gemini-3.8-flash-tts")
        self.assertEqual(self.kwargs["instructions"], "voce sussurrata")

    def test_get_defaults_stay_backwards_compatible(self):
        status, _ = call("GET", "/audio/tts_get", query="text=ciao")
        self.assertEqual(status, 200)
        self.assertEqual(self.kwargs["model"], "openai-audio")
        self.assertIsNone(self.kwargs["instructions"])

    def test_post_forwards_instructions(self):
        body = b'{"text": "ciao", "voice": "alloy", "instructions": "tono ironico"}'
        status, _ = call("POST", "/audio/tts_post", body=body)
        self.assertEqual(status, 200)
        self.assertEqual(self.kwargs["instructions"], "tono ironico")


class TtsWavFallbackTests(unittest.TestCase):
    """Ripiego mp3 -> wav con conversione, e cosa succede se la conversione fallisce."""

    def make_client(self):
        return PollinationsClient(
            image_api_url="http://unused", text_api_url="http://unused")

    def test_retries_in_wav_and_converts_back_to_mp3(self):
        client = self.make_client()
        calls = []

        async def fake_request(method, url, headers=None, json_data=None, timeout=None):
            calls.append(dict(json_data))
            if json_data.get("response_format") == "mp3":
                return 400, None, "application/json", "unsupported response_format for this model"
            return 200, b"RIFFwav-bytes", "audio/wav", None

        async def fake_convert(data, input_format, output_format):
            self.assertEqual((input_format, output_format), ("wav", "mp3"))
            handle = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
            handle.write(b"ID3converted")
            handle.close()
            self.addCleanup(os.unlink, handle.name)
            return handle.name

        client._make_request = fake_request
        client._convert_audio_data = fake_convert

        status, data, content_type, error = asyncio.run(
            client._make_tts_api_call("ciao", "alloy", "google/gemini-3.8-flash-tts", "mp3", False, None))

        self.assertEqual(status, 200)
        self.assertEqual(data, b"ID3converted")
        self.assertEqual(content_type, "audio/mpeg")
        self.assertEqual([c.get("response_format") for c in calls], ["mp3", "wav"])

    def test_returns_original_audio_when_conversion_fails(self):
        client = self.make_client()

        async def fake_request(method, url, headers=None, json_data=None, timeout=None):
            if json_data.get("response_format") == "mp3":
                return 400, None, "application/json", "model only supports wav response_format"
            return 200, b"RIFFwav-bytes", "audio/wav", None

        async def failing_convert(data, input_format, output_format):
            return None

        client._make_request = fake_request
        client._convert_audio_data = failing_convert

        status, data, content_type, _ = asyncio.run(
            client._make_tts_api_call("ciao", "alloy", "qualche/tts-wav", "mp3", False, None))

        self.assertEqual(status, 200)
        self.assertEqual(data, b"RIFFwav-bytes")
        self.assertEqual(content_type, "audio/wav")

    def test_instructions_reach_the_payload(self):
        client = self.make_client()
        seen = {}

        async def fake_request(method, url, headers=None, json_data=None, timeout=None):
            seen.update(json_data or {})
            return 200, b"ID3fake", "audio/mpeg", None

        client._make_request = fake_request
        asyncio.run(client._make_tts_api_call(
            "ciao", "alloy", "openai-audio", "mp3", False, None, instructions="sussurrato"))

        self.assertEqual(seen.get("instructions"), "sussurrato")

    def test_no_instructions_key_when_absent(self):
        """Non mandare `instructions: None` a upstream: alcuni lo rifiutano."""
        client = self.make_client()
        seen = {}

        async def fake_request(method, url, headers=None, json_data=None, timeout=None):
            seen.update(json_data or {})
            return 200, b"ID3fake", "audio/mpeg", None

        client._make_request = fake_request
        asyncio.run(client._make_tts_api_call("ciao", "alloy", "openai-audio", "mp3", False, None))

        self.assertNotIn("instructions", seen)


if __name__ == "__main__":
    unittest.main()
