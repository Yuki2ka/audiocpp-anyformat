#!/usr/bin/env python3
"""Stub backend that mimics audiocpp_server's audio gates, for testing
the anyformat proxy without building C++:

* POST /v1/ui/upload        -> stages bytes, checks nothing (like the real server)
* POST /v1/audio/transcriptions
    - multipart: applies the C++ rule is_wav_upload_filename (ext .wav or empty)
    - json: reads the "audio" path and requires RIFF/WAVE content
"""
import json
import os
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

AUDIO_PATH_KEYS = ("audio", "audio_path", "file")


def is_wav_upload_filename(filename: str) -> bool:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext == "" or ext == "wav"


def is_wav_bytes(data: bytes) -> bool:
    return len(data) >= 12 and data[0:4] == b"RIFF" and data[8:12] == b"WAVE"


class StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[stub] " + (fmt % args) + "\n")

    def _reply(self, status: int, obj: dict):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            body = b"<html><body>stub WebUI</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        ctype = self.headers.get("Content-Type", "")

        if self.path == "/v1/ui/upload":
            # Real server: writes bytes verbatim to a temp file, no
            # content check, and returns that path.
            name = self.headers.get("X-AudioCPP-Filename", "audio.wav")
            staged = os.path.join(tempfile.gettempdir(), name)
            with open(staged, "wb") as fh:
                fh.write(body)
            self.log_message("STAGED %s (%d bytes) riff=%s",
                             name, len(body), is_wav_bytes(body))
            self._reply(200, {"path": staged, "bytes": len(body)})
            return

        if self.path == "/v1/audio/transcriptions":
            if ctype.startswith("multipart/form-data"):
                boundary = ctype.split("boundary=")[-1].strip('"')
                parts = body.split(b"--" + boundary.encode())
                for part in parts:
                    if b'name="file"' not in part:
                        continue
                    head, _, payload = part.partition(b"\r\n\r\n")
                    payload = payload.rstrip(b"\r\n")
                    filename = "unknown"
                    for line in head.decode(errors="replace").split("\r\n"):
                        if line.lower().startswith("content-disposition:"):
                            for piece in line.split(";"):
                                if "filename=" in piece:
                                    filename = piece.split("=", 1)[1].strip().strip('"')
                    if not is_wav_upload_filename(filename):
                        self.log_message("GATE A REJECT (extension) %s", filename)
                        self._reply(400, {"error": {
                            "message": "only WAV audio uploads are currently supported"}})
                        return
                    if not is_wav_bytes(payload):
                        self.log_message("GATE B REJECT (content) %s", filename)
                        self._reply(400, {"error": {
                            "message": "input is MP4/M4A (AAC or ALAC), not WAV; convert it first"}})
                        return
                    self.log_message("MULTIPART OK %s (%d wav bytes)", filename, len(payload))
                    self._reply(200, {"text": "[stub transcript]"})
                    return
                self._reply(400, {"error": {"message": "no file part"}})
                return
            # JSON path
            doc = json.loads(body)
            audio_path = None
            for key in AUDIO_PATH_KEYS:
                if key in doc:
                    audio_path = doc[key]
                    break
            if not audio_path:
                self._reply(400, {"error": {"message": "missing audio"}})
                return
            try:
                with open(audio_path, "rb") as fh:
                    data = fh.read()
            except OSError as exc:
                self._reply(400, {"error": {"message": f"cannot read {audio_path}: {exc}"}})
                return
            if not is_wav_bytes(data):
                self.log_message("GATE B REJECT (content) %s", audio_path)
                self._reply(400, {"error": {
                    "message": "input is MP4/M4A (AAC or ALAC), not WAV; convert it first"}})
                return
            self.log_message("JSON OK %s (%d wav bytes)", audio_path, len(data))
            self._reply(200, {"text": "[stub transcript]"})
            return

        self._reply(404, {"error": "unknown endpoint: " + self.path})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8081
    ThreadingHTTPServer(("127.0.0.1", port), StubHandler).serve_forever()
