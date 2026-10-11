import base64
import hashlib
import http.client
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
from fixtures import make_pdf


class RenderTests(unittest.TestCase):
    def assert_rejected(self, source, code, **options):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(server.MediaError) as raised:
                server.render_pdf(source, temp_root=root, **options)
            self.assertEqual(raised.exception.code, code)
            self.assertEqual(set(raised.exception.payload()), {"error"})
            self.assertNotIn("pages", raised.exception.payload())
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_real_three_pages_and_stable_sha256(self):
        source = make_pdf(3)
        with tempfile.TemporaryDirectory() as root:
            first = server.render_pdf(source, temp_root=root)
            second = server.render_pdf(source, temp_root=root)
            self.assertEqual(list(Path(root).iterdir()), [])
        self.assertEqual(first, second)
        self.assertEqual(first["page_count"], 3)
        self.assertEqual(first["source_sha256"], hashlib.sha256(source).hexdigest())
        self.assertEqual([page["page"] for page in first["pages"]], [1, 2, 3])
        for page in first["pages"]:
            self.assertEqual(page["mime_type"], "image/jpeg")
            data = base64.b64decode(page["data_base64"], validate=True)
            self.assertTrue(data.startswith(b"\xff\xd8"))
            self.assertTrue(data.endswith(b"\xff\xd9"))
        self.assertLessEqual(sum(len(base64.b64decode(page["data_base64"])) for page in first["pages"]), server.MAX_JPEG_BYTES)

    def test_twelve_pages_are_numeric_not_lexicographic(self):
        output = server.render_pdf(make_pdf(12))
        self.assertEqual([page["page"] for page in output["pages"]], list(range(1, 13)))
        self.assertEqual(output["page_count"], 12)

    def test_real_corrupt_pdf(self):
        self.assert_rejected(b"%PDF-1.4\ncorrupt\n", "PDF_INVALID")

    def test_corrupt_last_page_never_returns_earlier_pages(self):
        self.assert_rejected(make_pdf(3, corrupt_last=True), "PDF_INVALID")

    def test_real_password_protected_pdf(self):
        self.assert_rejected(make_pdf(3, encrypted=True), "PDF_ENCRYPTED")

    def test_real_encrypted_pdf_with_empty_password_still_rejected(self):
        self.assert_rejected(make_pdf(3, encrypted=True, empty_password=True), "PDF_ENCRYPTED")

    def test_thirteenth_page_rejected_before_render(self):
        original_run = server._run
        commands = []

        def capture(arguments, *args):
            commands.append(arguments)
            return original_run(arguments, *args)

        with patch.object(server, "_run", side_effect=capture):
            self.assert_rejected(make_pdf(13), "INPUT_LIMIT_EXCEEDED")
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0][0], "/usr/bin/pdfinfo")

    def test_source_over_25_mib_rejected_before_temp_or_process(self):
        with patch.object(server.subprocess, "Popen") as process:
            self.assert_rejected(b"%PDF-" + b"x" * server.MAX_SOURCE_BYTES, "INPUT_LIMIT_EXCEEDED")
            process.assert_not_called()

    def test_render_timeout_kills_process_and_cleans_temp(self):
        real_popen = server.subprocess.Popen
        children = []

        def slow_render(arguments, **options):
            if arguments[0] == "/usr/bin/pdftoppm":
                arguments = [sys.executable, "-c", "import time; time.sleep(60)"]
            child = real_popen(arguments, **options)
            children.append(child)
            return child

        with patch.object(server, "RENDER_SECONDS", 0.5), patch.object(server.subprocess, "Popen", side_effect=slow_render):
            self.assert_rejected(make_pdf(3), "RENDER_TIMEOUT")
        self.assertEqual(len(children), 2)
        self.assertTrue(all(child.poll() is not None for child in children))

    def test_aggregate_real_jpeg_limit_rejects_whole_file(self):
        source = make_pdf(3)
        result = server.render_pdf(source)
        sizes = [len(base64.b64decode(page["data_base64"])) for page in result["pages"]]
        # Real JPEGs are small; lower the cap so the test stays fast while still exercising Poppler.
        with patch.object(server, "MAX_JPEG_BYTES", max(sizes) + 1):
            self.assert_rejected(source, "INPUT_LIMIT_EXCEEDED")

    def test_actual_five_mib_limit_during_process_rejects_and_cleans(self):
        real_popen = server.subprocess.Popen
        children = []

        def huge_render(arguments, **options):
            if arguments[0] == "/usr/bin/pdftoppm":
                arguments = [sys.executable, "-c",
                             "from pathlib import Path; import time; "
                             "Path('page-1.jpg').write_bytes(b'x' * (5 * 1024 * 1024 + 1)); time.sleep(60)"]
            child = real_popen(arguments, **options)
            children.append(child)
            return child

        with patch.object(server.subprocess, "Popen", side_effect=huge_render):
            self.assert_rejected(make_pdf(3), "INPUT_LIMIT_EXCEEDED")
        self.assertTrue(all(child.poll() is not None for child in children))

    def test_subprocess_failures_clean_request_directory(self):
        with patch.object(server.subprocess, "Popen", side_effect=OSError("fixture failure")):
            with tempfile.TemporaryDirectory() as root:
                with self.assertRaises(OSError):
                    server.render_pdf(make_pdf(3), temp_root=root)
                self.assertEqual(list(Path(root).iterdir()), [])

    def test_partial_renderer_output_rejected(self):
        original_run = server._run

        def remove_page(arguments, directory, deadline, label):
            result = original_run(arguments, directory, deadline, label)
            if label == "render":
                files = sorted(Path(directory).glob("page-*.jpg"))
                files[-1].unlink()
            return result

        with patch.object(server, "_run", side_effect=remove_page):
            self.assert_rejected(make_pdf(3), "PDF_INVALID")


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http = server.MediaServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.http.server_address[1]
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.thread.join()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, json.loads(response.read()), dict(response.getheaders())
        finally:
            connection.close()

    def headers_only(self, headers, path="/render-pdf"):
        # No body is sent: an immediate response proves rejection happens before buffering.
        with socket.create_connection(("127.0.0.1", self.port), timeout=2) as connection:
            connection.sendall((f"POST {path} HTTP/1.1\r\nHost: localhost\r\n" + headers + "\r\n").encode())
            response = http.client.HTTPResponse(connection)
            response.begin()
            return response.status, json.loads(response.read())

    def test_real_http_health(self):
        status, payload, headers = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"status": "ok", "media_version": server.MEDIA_VERSION})
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_real_http_three_page_render(self):
        source = make_pdf(3)
        status, payload, headers = self.request("POST", "/render-pdf", source, {"Content-Type": "application/pdf"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["page_count"], 3)
        self.assertEqual([page["page"] for page in payload["pages"]], [1, 2, 3])
        self.assertEqual(payload["source_sha256"], hashlib.sha256(source).hexdigest())
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_oversize_rejected_without_body(self):
        status, payload = self.headers_only(f"Content-Type: application/pdf\r\nContent-Length: {server.MAX_SOURCE_BYTES + 1}\r\n")
        self.assertEqual(status, 413)
        self.assertEqual(payload["error"]["code"], "INPUT_LIMIT_EXCEEDED")
        self.assertNotIn("pages", payload)

    def test_oversize_expect_continue_rejected_before_continue(self):
        status, payload = self.headers_only(f"Content-Type: application/pdf\r\nContent-Length: {server.MAX_SOURCE_BYTES + 1}\r\nExpect: 100-continue\r\n")
        self.assertEqual(status, 413)
        self.assertEqual(payload["error"]["code"], "INPUT_LIMIT_EXCEEDED")

    def test_wrong_mime_rejected_without_body(self):
        status, payload = self.headers_only("Content-Type: application/json\r\nContent-Length: 1000\r\n")
        self.assertEqual(status, 415)
        self.assertEqual(payload["error"]["code"], "FILE_UNSUPPORTED")

    def test_missing_duplicate_and_invalid_lengths_rejected(self):
        for headers in ["", "Content-Length: -1\r\n", "Content-Length: 1,1\r\n", "Content-Length: 4\r\nContent-Length: 4\r\n"]:
            with self.subTest(headers=headers):
                status, payload = self.headers_only("Content-Type: application/pdf\r\n" + headers)
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], "REQUEST_INVALID")

    def test_chunked_transfer_rejected_without_body(self):
        status, payload = self.headers_only("Content-Type: application/pdf\r\nTransfer-Encoding: chunked\r\n")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "REQUEST_INVALID")

    def test_raw_paths_and_urls_are_not_opened(self):
        for source in [b"/etc/passwd", b"http://127.0.0.1:20128/v1", b'{"url":"http://example.invalid/file.pdf"}']:
            with self.subTest(source=source):
                status, payload, _ = self.request("POST", "/render-pdf", source, {"Content-Type": "application/pdf"})
                self.assertEqual(status, 422)
                self.assertEqual(payload["error"]["code"], "PDF_INVALID")
                self.assertNotIn("pages", payload)

    def test_path_and_url_query_parameters_rejected_before_body(self):
        for path in ["/render-pdf?path=/etc/passwd", "/render-pdf?url=http://example.invalid/a.pdf", "http://example.invalid/render-pdf"]:
            with self.subTest(path=path):
                status, payload = self.headers_only("Content-Type: application/pdf\r\nContent-Length: 1000\r\n", path=path)
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], "REQUEST_INVALID")

    def test_http_error_has_no_partial_pages(self):
        for source, code, expected in [(make_pdf(3, corrupt_last=True), "PDF_INVALID", 422),
                                        (make_pdf(3, encrypted=True), "PDF_ENCRYPTED", 422),
                                        (make_pdf(13), "INPUT_LIMIT_EXCEEDED", 413)]:
            with self.subTest(code=code):
                status, payload, _ = self.request("POST", "/render-pdf", source, {"Content-Type": "application/pdf"})
                self.assertEqual(status, expected)
                self.assertEqual(payload["error"]["code"], code)
                self.assertEqual(set(payload), {"error"})

    def test_http_timeout_and_jpeg_cap_return_only_error(self):
        source = make_pdf(3)
        for setting, value, expected, code in [
            ("RENDER_SECONDS", 0, 504, "RENDER_TIMEOUT"),
            ("MAX_JPEG_BYTES", 1, 413, "INPUT_LIMIT_EXCEEDED"),
        ]:
            with self.subTest(setting=setting), patch.object(server, setting, value):
                status, payload, _ = self.request("POST", "/render-pdf", source, {"Content-Type": "application/pdf"})
                self.assertEqual(status, expected)
                self.assertEqual(payload["error"]["code"], code)
                self.assertEqual(set(payload), {"error"})

    def test_truncated_upload_is_not_rendered(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=2) as connection:
            connection.sendall(b"POST /render-pdf HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/pdf\r\nContent-Length: 1000\r\n\r\n%PDF-")
            connection.shutdown(socket.SHUT_WR)
            response = http.client.HTTPResponse(connection)
            response.begin()
            self.assertEqual(response.status, 400)
            payload = json.loads(response.read())
            self.assertEqual(payload["error"]["code"], "REQUEST_INVALID")


if __name__ == "__main__":
    unittest.main()
