"""Helper PDF stateless; tidak menerima path/URL dan tidak mencatat isi submission."""

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import tempfile
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

MAX_SOURCE_BYTES = 25 * 1024 * 1024
MAX_PAGES = 12
MAX_JPEG_BYTES = 5 * 1024 * 1024
# Skala 1200 px / kualitas 70 (media-v3): payload lebih kecil; WAJIB uji ulang keterbacaan,
# kembali ke 1600 bila tulisan tangan mulai sulit dibaca.
RENDER_SCALE_PX = 1200
RENDER_JPEG_QUALITY = 70
RENDER_SECONDS = 45
UPLOAD_SECONDS = 15
DIAGNOSTIC_BYTES = 64 * 1024
MEDIA_VERSION = "media-v3"

MESSAGES = {
    "INPUT_LIMIT_EXCEEDED": "Batas berkas, halaman, atau gambar terlampaui; unggah ulang versi lebih kecil.",
    "PDF_INVALID": "PDF rusak atau tidak dapat dirender utuh; unggah ulang PDF yang valid.",
    "PDF_ENCRYPTED": "PDF terenkripsi tidak didukung; unggah ulang PDF tanpa enkripsi.",
    "RENDER_TIMEOUT": "Render seluruh PDF melewati 45 detik; unggah ulang versi lebih sederhana.",
    "FILE_UNSUPPORTED": "Endpoint hanya menerima byte mentah application/pdf.",
    "REQUEST_INVALID": "Request memerlukan satu Content-Length valid, tanpa URL/path atau Transfer-Encoding.",
    "UPLOAD_TIMEOUT": "Pengiriman PDF melewati batas waktu.",
    "MEDIA_INTERNAL": "Helper gagal memproses berkas; tidak ada halaman yang dikembalikan.",
}


class MediaError(Exception):
    def __init__(self, code, status):
        super().__init__(MESSAGES[code])
        self.code = code
        self.status = status

    def payload(self):
        return {"error": {"code": self.code, "message": MESSAGES[self.code]}}


def _jpeg_files(directory):
    return list(Path(directory).glob("page-*.jpg"))


def _check_output_limit(directory):
    if sum(path.stat().st_size for path in _jpeg_files(directory)) > MAX_JPEG_BYTES:
        raise MediaError("INPUT_LIMIT_EXCEEDED", 413)


def _stop(process):
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait()


def _run(arguments, directory, deadline, label):
    """Batasi waktu/diagnostik/output selama proses berjalan, bukan sesudah buffering."""
    out_path = Path(directory) / (label + ".stdout")
    err_path = Path(directory) / (label + ".stderr")
    with out_path.open("wb") as stdout, err_path.open("wb") as stderr:
        process = subprocess.Popen(
            arguments, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
            cwd=directory, shell=False, start_new_session=True,
            env={"LC_ALL": "C", "PATH": "/usr/bin:/bin", "HOME": directory},
        )
        try:
            while True:
                if time.monotonic() >= deadline:
                    raise MediaError("RENDER_TIMEOUT", 504)
                _check_output_limit(directory)
                if out_path.stat().st_size + err_path.stat().st_size > DIAGNOSTIC_BYTES:
                    raise MediaError("PDF_INVALID", 422)
                if process.poll() is not None:
                    break
                time.sleep(min(0.02, max(0, deadline - time.monotonic())))
        finally:
            _stop(process)
    # Tutup race proses selesai tepat di antara stat() dan poll().
    _check_output_limit(directory)
    if out_path.stat().st_size + err_path.stat().st_size > DIAGNOSTIC_BYTES:
        raise MediaError("PDF_INVALID", 422)
    if time.monotonic() >= deadline:
        raise MediaError("RENDER_TIMEOUT", 504)
    return process.returncode, out_path.read_bytes(), err_path.read_bytes()


def _render_path(source_path, directory):
    deadline = time.monotonic() + RENDER_SECONDS
    source_size = source_path.stat().st_size
    if source_size > MAX_SOURCE_BYTES:
        raise MediaError("INPUT_LIMIT_EXCEEDED", 413)
    with source_path.open("rb") as source:
        if not source.read(8).startswith(b"%PDF-"):
            raise MediaError("PDF_INVALID", 422)
        source.seek(0)
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    status, info, errors = _run(
        ["/usr/bin/pdfinfo", str(source_path)], directory, deadline, "info",
    )
    # Password-protected PDF gagal sebelum pdfinfo dapat mencetak metadata.
    if re.search(rb"password|encrypted", errors, re.IGNORECASE):
        raise MediaError("PDF_ENCRYPTED", 422)
    if re.search(rb"^Encrypted:\s+yes\b", info, re.MULTILINE):
        raise MediaError("PDF_ENCRYPTED", 422)
    # Poppler kadang melakukan repair sambil exit 0; jangan menerima PDF parsial.
    if status != 0 or errors.strip():
        raise MediaError("PDF_INVALID", 422)
    match = re.search(rb"^Pages:\s+(\d+)\s*$", info, re.MULTILINE)
    if not match or int(match[1]) < 1:
        raise MediaError("PDF_INVALID", 422)
    page_count = int(match[1])
    if page_count > MAX_PAGES:
        raise MediaError("INPUT_LIMIT_EXCEEDED", 413)
    status, _, errors = _run(
        ["/usr/bin/pdftoppm", "-scale-to", str(RENDER_SCALE_PX), "-jpeg", "-jpegopt",
         f"quality={RENDER_JPEG_QUALITY}", str(source_path), str(Path(directory) / "page")],
        directory, deadline, "render",
    )
    _check_output_limit(directory)
    if status != 0 or errors.strip():
        raise MediaError("PDF_INVALID", 422)
    files = {}
    for path in _jpeg_files(directory):
        match = re.fullmatch(r"page-(\d+)\.jpg", path.name)
        if not match or int(match[1]) in files:
            raise MediaError("PDF_INVALID", 422)
        files[int(match[1])] = path
    if sorted(files) != list(range(1, page_count + 1)):
        raise MediaError("PDF_INVALID", 422)
    pages = []
    # Semua output sudah lengkap dan ukurannya sah sebelum satu halaman pun dikirim.
    for page in range(1, page_count + 1):
        data = files[page].read_bytes()
        if len(data) < 4 or not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
            raise MediaError("PDF_INVALID", 422)
        pages.append({"page": page, "mime_type": "image/jpeg",
                      "data_base64": base64.b64encode(data).decode("ascii")})
    if time.monotonic() >= deadline:
        raise MediaError("RENDER_TIMEOUT", 504)
    return {"page_count": page_count, "source_sha256": digest, "pages": pages}


def render_pdf(source, temp_root=None):
    """Entry point byte untuk pengujian; HTTP menulis streaming ke tempfile."""
    if len(source) > MAX_SOURCE_BYTES:
        raise MediaError("INPUT_LIMIT_EXCEEDED", 413)
    with tempfile.TemporaryDirectory(prefix="p2-media-", dir=temp_root) as directory:
        source_path = Path(directory) / "source.pdf"
        source_path.write_bytes(source)
        return _render_path(source_path, directory)


class MediaServer(HTTPServer):
    # Serial: hanya satu render/source aktif; backlog dan jumlah proses juga dibatasi.
    request_queue_size = 4
    allow_reuse_address = True

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(UPLOAD_SECONDS)
        return connection, address

    def handle_error(self, request, client_address):
        # Jangan cetak traceback yang dapat memuat lokasi/isi submission.
        pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Project2Media"
    sys_version = ""

    def log_message(self, format, *args):
        pass

    def _reply(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass

    def _source_length(self):
        if self.path != "/render-pdf" or self.command != "POST":
            raise MediaError("REQUEST_INVALID", 400)
        if self.headers.get_all("Transfer-Encoding"):
            raise MediaError("REQUEST_INVALID", 400)
        content_types = self.headers.get_all("Content-Type", [])
        if len(content_types) != 1 or content_types[0].strip().lower() != "application/pdf":
            raise MediaError("FILE_UNSUPPORTED", 415)
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,20}", lengths[0]):
            raise MediaError("REQUEST_INVALID", 400)
        length = int(lengths[0])
        if length > MAX_SOURCE_BYTES:
            raise MediaError("INPUT_LIMIT_EXCEEDED", 413)
        if length == 0:
            raise MediaError("PDF_INVALID", 422)
        return length

    def handle_expect_100(self):
        try:
            self._source_length()
        except MediaError as error:
            self._reply(error.status, error.payload())
            return False
        self.send_response_only(100)
        self.end_headers()
        return True

    def do_GET(self):
        if self.path != "/health":
            self._reply(404, {"error": {"code": "NOT_FOUND", "message": "Endpoint tidak ditemukan."}})
            return
        self._reply(200, {"status": "ok", "media_version": MEDIA_VERSION})

    def do_POST(self):
        try:
            length = self._source_length()  # Validasi sebelum read/buffer body.
            with tempfile.TemporaryDirectory(prefix="p2-media-") as directory:
                source_path = Path(directory) / "source.pdf"
                remaining = length
                deadline = time.monotonic() + UPLOAD_SECONDS
                with source_path.open("wb") as source:
                    while remaining:
                        seconds = deadline - time.monotonic()
                        if seconds <= 0:
                            raise MediaError("UPLOAD_TIMEOUT", 408)
                        self.connection.settimeout(seconds)
                        chunk = self.rfile.read1(min(64 * 1024, remaining))
                        if not chunk:
                            raise MediaError("REQUEST_INVALID", 400)
                        source.write(chunk)
                        remaining -= len(chunk)
                payload = _render_path(source_path, directory)
            self.connection.settimeout(UPLOAD_SECONDS)
            self._reply(200, payload)
        except MediaError as error:
            self._reply(error.status, error.payload())
        except socket.timeout:
            self._reply(408, MediaError("UPLOAD_TIMEOUT", 408).payload())
        except (OSError, ValueError):
            self._reply(500, MediaError("MEDIA_INTERNAL", 500).payload())


if __name__ == "__main__":
    os.umask(0o077)
    with MediaServer(("0.0.0.0", 8080), Handler) as server:
        server.serve_forever()
