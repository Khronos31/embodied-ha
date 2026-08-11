import json
import os
import stat
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "embodied_ha"))
os.environ.setdefault("HA_URL", "http://homeassistant.invalid")

import setup_terms
from web import server


class SetupTermsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "setup_terms_consent.json"
        self._env = mock.patch.dict(
            os.environ, {"EHA_SETUP_TERMS_FILE": str(self.path)}, clear=False
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_fresh_setup_requires_explicit_consent(self):
        with mock.patch.object(
            setup_terms.harness_state, "read_selection", return_value=("missing", None)
        ):
            status = setup_terms.public_status()
        self.assertTrue(status["required"])
        self.assertFalse(status["accepted"])
        self.assertFalse(status["grandfathered"])
        self.assertEqual(status["version"], setup_terms.CONSENT_VERSION)
        self.assertEqual(len(status["terms"]), 4)

    def test_accept_is_atomic_private_and_round_trips(self):
        with mock.patch.object(
            setup_terms.harness_state, "read_selection", return_value=("missing", None)
        ), mock.patch.object(setup_terms.os, "replace", wraps=os.replace) as replace:
            status = setup_terms.accept(setup_terms.CONSENT_VERSION)

        replace.assert_called_once()
        self.assertTrue(status["accepted"])
        self.assertFalse(status["required"])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        record = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(record["version"], setup_terms.CONSENT_VERSION)
        self.assertEqual(record["statement"], setup_terms.CONSENT_STATEMENT)
        self.assertTrue(record["accepted_at"].endswith("+00:00"))

    def test_wrong_or_stale_version_is_rejected(self):
        with self.assertRaises(ValueError):
            setup_terms.accept("stale-version")
        self.assertFalse(self.path.exists())

    def test_selected_existing_installation_is_grandfathered(self):
        with mock.patch.object(
            setup_terms.harness_state, "read_selection", return_value=("valid", "claude")
        ):
            status = setup_terms.public_status()
        self.assertFalse(status["required"])
        self.assertFalse(status["accepted"])
        self.assertTrue(status["grandfathered"])

    def test_invalid_record_fails_closed_for_fresh_setup(self):
        self.path.write_text('{"accepted": true, "version": "old"}', encoding="utf-8")
        with mock.patch.object(
            setup_terms.harness_state, "read_selection", return_value=("missing", None)
        ):
            self.assertTrue(setup_terms.is_required())


class SetupTermsEndpointTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.path = base / "setup_terms_consent.json"
        self.flag = base / "selected_harness"
        self._env = mock.patch.dict(
            os.environ,
            {
                "EHA_SETUP_GUARD": "off",
                "EHA_SETUP_TERMS_FILE": str(self.path),
                "EHA_HARNESS_FLAG_FILE": str(self.flag),
            },
            clear=False,
        )
        self._env.start()
        self.addCleanup(self._env.stop)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.thread.join(timeout=5)
        self.httpd.server_close()

    def _json(self, path: str, body: dict | None = None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            self.base_url + path, data=data, method="GET" if data is None else "POST"
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())

    def test_get_accept_and_direct_api_gate(self):
        status, payload = self._json("/api/setup/terms")
        self.assertEqual(status, 200)
        self.assertTrue(payload["required"])

        request = urllib.request.Request(
            self.base_url + "/api/setup/codex/install", data=b"{}", method="POST"
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(raised.exception.code, 428)
        self.assertEqual(json.loads(raised.exception.read())["version"], setup_terms.CONSENT_VERSION)

        status, payload = self._json(
            "/api/setup/terms",
            {"accepted": True, "version": setup_terms.CONSENT_VERSION},
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["accepted"])
        self.assertFalse(payload["required"])

        handler = object.__new__(server.Handler)
        handler.send_json = mock.Mock()
        self.assertFalse(handler._block_setup_without_terms("/api/setup/codex/install"))
        handler.send_json.assert_not_called()

    def test_false_or_stale_acknowledgement_is_rejected(self):
        for body, expected in (
            ({"accepted": False, "version": setup_terms.CONSENT_VERSION}, 400),
            ({"accepted": True, "version": "stale"}, 409),
        ):
            with self.subTest(body=body), self.assertRaises(urllib.error.HTTPError) as raised:
                self._json("/api/setup/terms", body)
            self.assertEqual(raised.exception.code, expected)
        self.assertFalse(self.path.exists())

    def test_every_install_and_login_route_is_gated(self):
        expected = {
            "/api/setup/login", "/api/setup/login-code",
            "/api/setup/claude/login", "/api/setup/claude/login-code",
            "/api/setup/claude/install",
            "/api/setup/antigravity/install", "/api/setup/antigravity/login",
            "/api/setup/antigravity/input", "/api/setup/antigravity/login-code",
            "/api/setup/codex/install", "/api/setup/codex/login",
        }
        self.assertEqual(server._SETUP_TERMS_GATED_PATHS, expected)


if __name__ == "__main__":
    unittest.main()
