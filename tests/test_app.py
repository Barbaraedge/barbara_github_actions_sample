import threading
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer
from unittest.mock import patch

from app.main import Handler, read_full_version, read_git_short_sha, read_version


class AppServerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def fetch(self, path: str):
        return urllib.request.urlopen(f"{self.base_url}{path}", timeout=5)

    def test_root_returns_expected_message_with_version(self):
        expected_version = read_full_version()

        with self.fetch("/") as response:
            body = response.read().decode("utf-8")

        self.assertEqual(response.status, 200)
        self.assertEqual(body, f"Everything works fine, version {expected_version}\n")

    def test_health_returns_ok(self):
        with self.fetch("/health") as response:
            body = response.read().decode("utf-8")

        self.assertEqual(response.status, 200)
        self.assertEqual(body, "OK\n")

    def test_favicon_returns_no_content(self):
        with self.fetch("/favicon.ico") as response:
            body = response.read()

        self.assertEqual(response.status, 204)
        self.assertEqual(body, b"")

    def test_unknown_path_returns_not_found(self):
        with self.assertRaises(urllib.error.HTTPError) as context:
            self.fetch("/missing")

        self.assertEqual(context.exception.code, 404)

    @patch("app.main.os.environ", {"BARBARA_APP_VERSION": "1.2.3"})
    def test_read_version_reads_env_var(self):
        self.assertEqual(read_version(), "1.2.3")

    def test_read_version_falls_back_to_unknown(self):
        with patch.dict("app.main.os.environ", {}, clear=True):
            self.assertEqual(read_version(), "unknown")

    @patch("app.main.os.environ", {"BARBARA_GIT_SHA_SHORT": "abc1234"})
    def test_read_git_short_sha_prefers_environment_variable(self):
        self.assertEqual(read_git_short_sha(), "abc1234")

    @patch("app.main.subprocess.run")
    def test_read_git_short_sha_reads_git_when_env_is_missing(self, mock_run):
        mock_run.return_value.stdout = "17a0484\n"

        self.assertEqual(read_git_short_sha(), "17a0484")

    @patch("app.main.read_git_short_sha", return_value="17a0484")
    @patch("app.main.os.environ", {"BARBARA_APP_VERSION": "1.2.3"})
    def test_read_full_version_appends_git_short_sha(self, _mock_sha):
        self.assertEqual(read_full_version(), "1.2.3+17a0484")

    @patch("app.main.read_git_short_sha", return_value=None)
    @patch("app.main.os.environ", {"BARBARA_APP_VERSION": "1.2.3"})
    def test_read_full_version_falls_back_to_plain_version(self, _mock_sha):
        self.assertEqual(read_full_version(), "1.2.3")


if __name__ == "__main__":
    unittest.main()
