"""Test degli endpoint decisions (/alpha/decisions, /v1/systemone) e del catalogo.

Niente rete: il client upstream viene sostituito con un finto che registra cosa
gli arriva. L'obiettivo è doppio: verificare il passthrough e impedire che il
default torni a essere un modello paid_only (è l'errore che la pre-review di
Pollinations ha già segnalato una volta sui modelli immagine).

Esecuzione:  venv/bin/python -m unittest discover -s tests -v
"""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server
import config

server.LOCAL_API_TOKEN = None

FREE_MODEL = "microsoft/microsoft-decision-1.1"


def call(method, path, body=b"", extra_headers=None):
    """Chiama server.app via ASGI. Ritorna (status, body_bytes)."""
    result = {}
    sent_body = bytearray()
    messages = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            result["status"] = message["status"]
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
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"testserver"), (b"content-type", b"application/json")]
        + list(extra_headers or []),
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8083),
    }
    asyncio.run(server.app(scope, receive, send))
    return result.get("status"), bytes(sent_body)


def valid_body(**overrides):
    body = {
        "state": "My payouts have been failing for 3 days.",
        "questions": {
            "department": {
                "type": "choice",
                "instructions": "Which team should handle this?",
                "criteria": {"billing": "Payment issues", "technical": "Product failures"},
            },
            "is_urgent": {"type": "noul", "instructions": "Does this convey urgency?"},
        },
    }
    body.update(overrides)
    return body


class DecisionEndpointTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self._real_proxy = server.client.proxy_json
        self._real_list_all = server.client.list_all_models

        async def fake_proxy_json(method, path, payload=None, stream=False):
            self.calls.append({"method": method, "path": path, "payload": payload})
            return 200, {"id": "dec-1", "answers": {}}, "application/json", None

        server.client.proxy_json = fake_proxy_json

    def tearDown(self):
        server.client.proxy_json = self._real_proxy
        server.client.list_all_models = self._real_list_all

    def post(self, path, body):
        return call("POST", path, body=json.dumps(body).encode())

    # --- passthrough ---

    def test_forwards_to_alpha_decisions(self):
        status, data = self.post("/alpha/decisions", valid_body())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["id"], "dec-1")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["path"], "/alpha/decisions")
        self.assertEqual(self.calls[0]["method"], "post")

    def test_default_model_is_free(self):
        self.post("/alpha/decisions", valid_body())
        self.assertEqual(self.calls[0]["payload"]["model"], FREE_MODEL)

    def test_default_model_is_not_paid_only(self):
        """Il default non deve essere uno dei modelli a pagamento del catalogo."""
        paid = {
            "typesafe/jev-1.14", "jev", "liquid/d1", "liquid/d1.2",
            "jaredpalmer/kev-4b", "perplexity/pplx-decider-v1.2",
            "openai/gpt-6-luna-decisions", "google/gemini-decision-1",
        }
        self.assertNotIn(FREE_MODEL, paid)
        self.assertNotIn(server.DECISION_DEFAULT_MODEL, paid)

    def test_explicit_model_is_passed_through(self):
        self.post("/alpha/decisions", valid_body(model="vllm-sr/d3"))
        self.assertEqual(self.calls[0]["payload"]["model"], "vllm-sr/d3")

    def test_systemone_alias_forwards_identically(self):
        status, _ = self.post("/v1/systemone", valid_body())
        self.assertEqual(status, 200)
        self.assertEqual(self.calls[0]["path"], "/alpha/decisions")

    # --- validazione ---

    def assert_rejected(self, body, expected_fragment):
        status, data = self.post("/alpha/decisions", body)
        self.assertEqual(status, 400, data)
        self.assertIn(expected_fragment, json.loads(data)["detail"])
        self.assertEqual(self.calls, [], "una richiesta invalida non deve raggiungere l'upstream")

    def test_missing_state_rejected(self):
        body = valid_body()
        del body["state"]
        self.assert_rejected(body, "state")

    def test_empty_questions_rejected(self):
        self.assert_rejected(valid_body(questions={}), "questions")

    def test_unknown_question_type_rejected(self):
        self.assert_rejected(
            valid_body(questions={"x": {"type": "ranking", "instructions": "nope"}}),
            "type must be one of")

    def test_missing_instructions_rejected(self):
        self.assert_rejected(
            valid_body(questions={"x": {"type": "noul"}}),
            "instructions is required")

    def test_invalid_json_rejected(self):
        status, _ = call("POST", "/alpha/decisions", body=b"{not json")
        self.assertEqual(status, 400)

    # --- errori upstream ---

    def test_upstream_status_is_preserved(self):
        async def rate_limited(method, path, payload=None, stream=False):
            return 429, None, None, "slow down"

        server.client.proxy_json = rate_limited
        status, data = self.post("/alpha/decisions", valid_body())
        self.assertEqual(status, 429)
        self.assertIn("slow down", json.loads(data)["detail"])


class DecisionCatalogTests(unittest.TestCase):
    def setUp(self):
        self._real_list_all = server.client.list_all_models
        self._real_decision_file = config.DECISION_MODELS_FILE
        self._real_all_file = config.ALL_MODELS_FILE
        self._real_openai_decision_file = None
        self.tmp = tempfile.TemporaryDirectory()
        config.DECISION_MODELS_FILE = str(Path(self.tmp.name) / "decision_models.json")
        config.ALL_MODELS_FILE = str(Path(self.tmp.name) / "all_models.json")

    def tearDown(self):
        server.client.list_all_models = self._real_list_all
        config.DECISION_MODELS_FILE = self._real_decision_file
        config.ALL_MODELS_FILE = self._real_all_file
        self.tmp.cleanup()

    def test_models_decision_filters_by_category(self):
        catalog = [
            {"name": "free-decider", "category": "decision", "paid_only": False},
            {"name": "paid-decider", "category": "decision", "paid_only": True},
            {"name": "some-image", "category": "image", "paid_only": False},
        ]

        async def fake_list(method=None):
            return 200, catalog, None

        server.client.list_all_models = fake_list
        status, data = call("GET", "/models/decision")
        self.assertEqual(status, 200)
        names = [m["name"] for m in json.loads(data)]
        self.assertEqual(names, ["free-decider", "paid-decider"])

    def test_models_decision_uses_cache(self):
        Path(config.DECISION_MODELS_FILE).write_text(
            json.dumps([{"name": "cached-decider", "category": "decision"}]))

        async def exploding_list():
            raise AssertionError("non deve chiamare l'upstream: la cache è valida")

        server.client.list_all_models = exploding_list
        status, data = call("GET", "/models/decision")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)[0]["name"], "cached-decider")


if __name__ == "__main__":
    unittest.main()
