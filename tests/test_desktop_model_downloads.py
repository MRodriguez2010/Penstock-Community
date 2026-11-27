"""Exercise explicit downloads against a small local HTTP fixture."""
from dataclasses import asdict
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))
import model_downloads
import models


class DownloadTests(unittest.TestCase):
    payload = b"GGUF0123456789abcdefghijklmnopqrstuvwxyz"
    first = 16
    etag = '"model-v1"'

    def setUp(self):
        self.responses = []
        self.requests = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                fixture.requests.append(dict(self.headers))
                status, headers, payload = fixture.responses.pop(0)
                self.send_response(status)
                self.send_header("Connection", "close")
                for name, value in headers.items():
                    self.send_header(name, str(value))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        # No upstream model request can pass this fixture's exact URL check.
        url = f"http://127.0.0.1:{server.server_port}/model"
        worker = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(worker.join, 2)
        self.addCleanup(server.shutdown)
        self.enterContext(patch.object(model_downloads, "trusted_download_url", lambda candidate: candidate == url))
        self.enterContext(patch.object(model_downloads, "MARGIN", 0))
        limits = asdict(models.PACKS["smollm3-3b"])
        limits.update(size=len(self.payload), sha256=sha256(self.payload).hexdigest(), url=url)
        self.pack = SimpleNamespace(**limits)
        self.destination = Path(self.enterContext(TemporaryDirectory())) / self.pack.filename
        self.stop = threading.Event()
        self.progress = []

    def download(self):
        return model_downloads.download(self.destination, self.pack, self.stop, self.progress.append)

    def interrupted(self, etag=etag):
        headers = {"Content-Length": self.pack.size}
        if etag is not None:
            headers["ETag"] = etag
        self.responses.append((200, headers, self.payload[:self.first]))
        with self.assertRaises(model_downloads.DownloadInterrupted):
            self.download()

    def partial_response(self, *, etag=etag, start=None, total=None):
        self.responses.append((206, {
            "Content-Range": f"bytes {start if start is not None else self.first}-{self.pack.size - 1}/{total or self.pack.size}",
            "Content-Length": self.pack.size - self.first,
            "ETag": etag,
        }, self.payload[self.first:]))

    def assert_clean(self):
        self.assertFalse(any(self.destination.parent.glob("*.download.*")))

    def test_retry_uses_exact_range_and_validator_after_short_response(self):
        self.interrupted()
        self.assertEqual(model_downloads.resume_bytes(self.destination, self.pack), self.first)
        partial, _ = model_downloads.staged_paths(self.destination)
        with partial.open("ab") as target:
            target.write(b"abandoned")
        self.partial_response()
        self.assertEqual(self.download()["resumedBytes"], self.first)
        self.assertNotIn("Range", self.requests[0])
        self.assertEqual(self.requests[1]["Range"], f"bytes={self.first}-")
        self.assertEqual(self.requests[1]["If-Range"], self.etag)
        self.assertEqual(self.requests[1]["Accept-Encoding"], "identity")
        self.assertEqual(self.destination.read_bytes(), self.payload)
        self.assertEqual(self.progress[-1], self.pack.size)
        self.assert_clean()

    def test_host_full_body_fallback_starts_from_first_byte(self):
        self.interrupted()
        self.responses.append((200, {"Content-Length": self.pack.size, "ETag": '"new"'}, self.payload))
        self.assertEqual(self.download()["resumedBytes"], 0)
        self.assertEqual(self.requests[1]["Range"], f"bytes={self.first}-")
        self.assertEqual(self.destination.read_bytes(), self.payload)
        self.assert_clean()

    def test_inconsistent_checkpoint_rejected_without_network(self):
        self.interrupted()
        partial, _ = model_downloads.staged_paths(self.destination)
        partial.write_bytes(b"GGUF" + b"z" * (self.first - 4))
        with self.assertRaisesRegex(model_downloads.DownloadProtocolError, "prefix checksum"):
            self.download()
        self.assertEqual(len(self.requests), 1)
        self.assert_clean()

    def test_wrong_model_revision_rejected_without_network(self):
        self.interrupted()
        self.pack.revision = "another-revision"
        with self.assertRaisesRegex(model_downloads.DownloadProtocolError, "checkpoint is inconsistent"):
            self.download()
        self.assertEqual(len(self.requests), 1)
        self.assert_clean()

    def test_inconsistent_range_or_etag_is_never_appended(self):
        for controls in ({"start": self.first + 1}, {"etag": '"changed"'}, {"total": self.pack.size + 1}):
            with self.subTest(**controls):
                self.interrupted()
                self.partial_response(**controls)
                with self.assertRaises(model_downloads.DownloadProtocolError):
                    self.download()
                self.assertFalse(self.destination.exists())
                self.assert_clean()

    def test_incomplete_response_requires_a_strong_etag_to_save(self):
        for etag in (None, 'W/"weak"'):
            with self.subTest(etag=etag):
                self.interrupted(etag)
                self.assertEqual(model_downloads.resume_bytes(self.destination, self.pack), 0)
                self.assert_clean()

    def test_rejected_range_starts_the_next_retry_clean(self):
        self.interrupted()
        self.responses.append((416, {"Content-Range": f"bytes */{self.pack.size}"}, b""))
        with self.assertRaisesRegex(model_downloads.DownloadProtocolError, "rejected the saved offset"):
            self.download()
        self.assert_clean()
        self.responses.append((200, {"Content-Length": self.pack.size}, self.payload))
        self.assertEqual(self.download()["resumedBytes"], 0)
        self.assertNotIn("Range", self.requests[-1])
        self.assert_clean()

    def test_checkpoint_after_a_second_interruption_extends_the_verified_prefix(self):
        self.interrupted()
        next_offset = self.first + 4
        self.partial_response()
        status, headers, _ = self.responses.pop()
        self.responses.append((status, headers, self.payload[self.first:next_offset]))
        with self.assertRaises(model_downloads.DownloadInterrupted):
            self.download()
        self.assertEqual(model_downloads.resume_bytes(self.destination, self.pack), next_offset)
        self.responses.append((206, {
            "Content-Range": f"bytes {next_offset}-{self.pack.size - 1}/{self.pack.size}",
            "Content-Length": self.pack.size - next_offset,
            "ETag": self.etag,
        }, self.payload[next_offset:]))
        self.assertEqual(self.download()["resumedBytes"], next_offset)
        self.assertEqual(self.destination.read_bytes(), self.payload)
        self.assert_clean()

    def test_failed_full_digest_never_replaces_existing_cache(self):
        self.destination.write_bytes(b"existing model")
        invalid = self.payload[:-1] + b"!"
        self.responses.append((200, {"Content-Length": self.pack.size, "ETag": self.etag}, invalid))
        with self.assertRaisesRegex(model_downloads.DownloadProtocolError, "pinned SHA-256"):
            self.download()
        self.assertEqual(self.destination.read_bytes(), b"existing model")
        self.assert_clean()

    def test_cancel_keeps_prefix_and_explicit_discard_removes_it(self):
        self.responses.append((200, {"Content-Length": self.pack.size, "ETag": self.etag},
                               self.payload[:self.first]))
        with self.assertRaisesRegex(model_downloads.DownloadInterrupted, "cancelled"):
            model_downloads.download(self.destination, self.pack, self.stop, lambda _value: self.stop.set())
        self.assertEqual(model_downloads.resume_bytes(self.destination, self.pack), self.first)
        model_downloads.discard(self.destination)
        self.assertEqual(model_downloads.resume_bytes(self.destination, self.pack), 0)
        self.assert_clean()


if __name__ == "__main__":
    unittest.main()
