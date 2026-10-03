"""Regression coverage for local settings and completed exports."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

import app
import config


class AuthorUiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        for mocked in (
            patch.object(config, "CONFIG_PATH", self.home / "config.json"),
            patch.object(app.Path, "home", return_value=self.home),
            patch.object(app.providers, "GROQ_KEY", ""),
            patch.object(app, "_ALLOW_REMOTE_ADMIN", False),
        ):
            mocked.start()
            self.addCleanup(mocked.stop)
        self.client = TestClient(app.app, client=("127.0.0.1", 12345))

    def test_key_is_persisted_and_used_without_returning_secret(self):
        response = self.client.put("/api/instructor/api-key", json={"api_key": " test-key "})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"configured": True})
        self.assertEqual(config.get("groq_api_key"), "test-key")
        self.assertEqual(app.providers.GROQ_KEY, "test-key")
        if config.os.name == "posix":
            self.assertEqual(config.CONFIG_PATH.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("test-key", self.client.get("/api/instructor/api-key").text)
        self.assertNotIn("test-key", self.client.get("/api/instructor/config").text)
        self.client.patch("/api/instructor/config", json={"instructor_url": "http://192.168.1.2:8077"})
        self.assertEqual(config.get("groq_api_key"), "test-key")

    def test_blank_key_preserves_previous_key(self):
        self.client.put("/api/instructor/api-key", json={"api_key": "test-key"})
        response = self.client.put("/api/instructor/api-key", json={"api_key": " "})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(app.providers.GROQ_KEY, "test-key")

    def test_saved_key_is_used_by_authoring_provider(self):
        self.client.put("/api/instructor/api-key", json={"api_key": "test-key"})
        response = Mock(status_code=200)
        response.json.return_value = {"choices": [{"message": {"content": "test response"}}]}
        with patch.object(app.providers.requests, "post", return_value=response) as request:
            result = app.providers.gen_groq("test-model", "test prompt")
        self.assertEqual(result["text"], "test response")
        self.assertEqual(request.call_args.kwargs["headers"]["Authorization"], "Bearer test-key")

    def test_invalid_key_and_storage_failure_preserve_previous_key(self):
        self.client.put("/api/instructor/api-key", json={"api_key": "original-key"})
        for key in ("bad key", "bad\nkey", "x" * 513):
            self.assertEqual(self.client.put("/api/instructor/api-key", json={"api_key": key}).status_code, 400)
        with patch.object(config.os, "replace", side_effect=PermissionError):
            response = self.client.put("/api/instructor/api-key", json={"api_key": "replacement-key"})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(config.get("groq_api_key"), "original-key")
        self.assertEqual(app.providers.GROQ_KEY, "original-key")
        self.assertEqual(list(self.home.iterdir()), [config.CONFIG_PATH])

    def test_password_and_local_gates_protect_keys(self):
        config.set("author_password_sha256", app._sha256("secret"))
        self.assertEqual(self.client.put("/api/instructor/api-key", json={"api_key": "test"}).status_code, 401)
        self.assertEqual(self.client.put("/api/instructor/api-key", json={"api_key": "test"}, headers={"X-Author-Key": "secret"}).status_code, 200)
        remote = TestClient(app.app, client=("192.168.1.5", 12345))
        self.assertEqual(remote.get("/api/instructor/api-key", headers={"X-Author-Key": "secret"}).status_code, 403)
        self.assertEqual(remote.post("/api/exports", json={"filename": "test.json", "content": "{}"}).status_code, 403)

    def test_exports_report_actual_filename_and_preserve_previous_file(self):
        for name in ("set.json", "assignment.json", "attempts.json", "analytics.csv"):
            payload = {"filename": name, "content": "student,score\nAda,1" if name.endswith("csv") else json.dumps({"test": "✓"})}
            response = self.client.post("/api/exports", json=payload)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"filename": name, "location": "Downloads"})
            self.assertEqual((self.home / "Downloads" / name).read_text(), payload["content"])
            duplicate = self.client.post("/api/exports", json={**payload, "content": "new"})
            self.assertEqual(duplicate.json()["filename"], f"{Path(name).stem} (1){Path(name).suffix}")
            self.assertEqual((self.home / "Downloads" / name).read_text(), payload["content"])

    def test_export_rejects_paths_and_other_extensions(self):
        for name in ("../bad.json", "/bad.csv", "sub\\bad.json", "bad.exe", "bad\x00.json"):
            self.assertEqual(self.client.post("/api/exports", json={"filename": name, "content": ""}).status_code, 400)

    def test_export_write_failure_is_not_reported_as_complete(self):
        with patch.object(Path, "open", side_effect=PermissionError):
            response = self.client.post("/api/exports", json={"filename": "set.json", "content": "{}"})
        self.assertEqual(response.status_code, 500)


if __name__ == "__main__":
    unittest.main()
