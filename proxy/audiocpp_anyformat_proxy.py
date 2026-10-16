#!/usr/bin/env python3
"""
audiocpp-anyformat proxy -- open ANY audio format in the audio.cpp WebUI dialog.

WHY THIS EXISTS
---------------
audio.cpp's C++ core only parses WAV files (src/framework/audio/wav_reader.cpp).
The server additionally rejects multipart uploads whose filename does not end
in .wav (app/server/server_protocol.cpp: is_wav_upload_filename). So a .3gpp
voice note from an old Android phone cannot be opened in the WebUI file dialog
even though the browser happily shows it (accept="audio/*").

Recompiling the C++ side is one fix. This proxy is the other: a small Python
reverse proxy that sits between the browser/API client and audiocpp_server and
transcodes non-WAV audio to WAV with ffmpeg *before* the C++ server sees it.
No audio.cpp rebuild, no C++ edits, works on Windows/Linux/macOS.

LAYOUT
      browser  --(any format)-->  this proxy :8080  --(WAV only)-->  audiocpp_server :8081

WHAT IT CONVERTS
  * POST /v1/ui/upload          (WebUI "Choose file" staging endpoint)
  * POST /v1/audio/transcriptions        (multipart file part + JSON audio/audio_path/file)
  * POST /v1/audio/transcriptions/details
  * POST /v1/batches/transcriptions
  * POST /v1/audio/alignments
  * POST /v1/audio/speech         (voice_ref as path, or base64 payload)
  Anything else (UI pages, model management, TTS generation, ...) is passed
  through untouched.

The conversion preserves the source sample rate / channels (AMR-NB is 8 kHz
mono) -- the C++ models resample internally, exactly as they do for WAV
input. Output is always PCM 16-bit WAV, which read_wav_f32() accepts.

USAGE
  1. Start audio.cpp on a private port:
         audiocpp_server --ui --ui-management --backend cuda --port 8081
  2. Start this proxy in front:
         python3 audiocpp_anyformat_proxy.py --backend http://127.0.0.1:8081
     (Windows: python audiocpp_anyformat_proxy.py --backend http://127.0.0.1:8081)
  3. Open http://127.0.0.1:8080 -- the WebUI now accepts .3gpp/.3gp/.amr/
     .mp3/.m4a/.ogg/.flac/... in every file dialog.

Requires: Python 3.8+ (standard library only) and an ffmpeg binary with AMR
support (for .3gpp/.3gp/.amr). Check with:
    ffmpeg -hide_banner -decoders | findstr amr      (Windows)
    ffmpeg -hide_banner -decoders | grep amr         (Linux/macOS)
The gyan.dev / BtbN "full" Windows builds include AMR. If your ffmpeg lacks
AMR, .3gpp conversion fails with a clear error; everything else still works.
"""

from __future__ import annotations

import argparse
import base64
import email.parser
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

# Extensions this proxy converts to WAV. Anything not listed (and not
# sniffed as a known non-WAV container) is forwarded untouched so that
# legitimate non-audio JSON fields are never mangled.
TRANSCODE_EXTENSIONS = {
    # 3GPP / old Android voice notes -- the main interest
    ".3gpp", ".3gp", ".3g2", ".amr",
    # common containers / codecs
    ".mp3", ".m4a", ".mp4", ".aac", ".ogg", ".oga", ".opus",
    ".flac", ".wma", ".wavpack", ".aiff", ".aif", ".aifc", ".caf",
    ".mkv", ".webm", ".mov", ".avi", ".m4p", ".m4b",
}

# JSON keys whose string value may be a server-local audio file path.
# Deliberately NOT a generic "path" rewrite: workflow/task JSON uses
# "path" for models and other assets, which must never be touched.
AUDIO_PATH_KEYS = {"audio", "audio_path", "file"}

# Routes whose JSON bodies are scanned for AUDIO_PATH_KEYS / voice_ref.
JSON_SCAN_ROUTES = {
    "/v1/audio/transcriptions",
    "/v1/audio/transcriptions/details",
    "/v1/audio/alignments",
    "/v1/audio/speech",
}

MULTIPART_ROUTES = {
    "/v1/audio/transcriptions",
    "/v1/audio/transcriptions/details",
    "/v1/batches/transcriptions",
    "/v1/audio/alignments",
}

HOP_BY_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length",
}

MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # matches the C++ server limit


class ConversionError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Format sniffing (mirrors src/framework/audio/wav_reader.cpp)
# --------------------------------------------------------------------------

def sniff_container(data: bytes) -> str | None:
    """Return a short label for a known non-WAV container, or None."""
    if len(data) >= 12 and data[0:4] == b"RIFF" and data[8:12] == b"WAVE":
        return None  # genuine WAV
    if len(data) >= 4:
        if data[0:4] == b"fLaC":
            return "FLAC"
        if data[0:4] == b"OggS":
            return "Ogg (Vorbis/Opus)"
        if data[0:3] == b"ID3":
            return "MP3"
        if data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
            return "MP3"
        if data[4:8] == b"ftyp":
            return "MP4/M4A/3GPP (ISO BMFF)"
        if data[0:4] == b"FORM":
            return "AIFF"
        if data[0:4] == b"RF64":
            return "RF64"
        if data[0:4] == b"caff":
            return "CAF"
        if data[0:4] == b"\x1a\x45\xdf\xa3":
            return "Matroska/WebM"
    if data.startswith(b"#!AMR"):
        return "AMR"
    return None


def is_wav_bytes(data: bytes) -> bool:
    return len(data) >= 12 and data[0:4] == b"RIFF" and data[8:12] == b"WAVE"


def looks_like_audio_file(path_or_name: str, data: bytes | None = None) -> bool:
    """Decide whether this upload needs transcoding.

    Content first when available (a WAV renamed to .3gpp is fine as-is),
    then extension (cheap, and what the C++ multipart gate uses), then
    sniffing as fallback for extension-less or mismatched names.
    """
    if data is not None and is_wav_bytes(data):
        return False
    ext = os.path.splitext(path_or_name)[1].lower()
    if ext == ".wav":
        return False
    if ext in TRANSCODE_EXTENSIONS:
        return True
    if data is not None and sniff_container(data) is not None:
        return True
    return False


# --------------------------------------------------------------------------
# ffmpeg transcoding
# --------------------------------------------------------------------------

FFMPEG_BIN = "ffmpeg"


def ffmpeg_check() -> bool:
    """Verify ffmpeg exists and can decode AMR (needed for .3gpp)."""
    try:
        proc = subprocess.run(
            [FFMPEG_BIN, "-hide_banner", "-decoders"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "amrnb" in proc.stdout


def transcode_to_wav(data: bytes, label: str, ffmpeg_bin: str) -> bytes:
    """Convert encoded audio bytes to PCM16 WAV bytes via ffmpeg."""
    with tempfile.TemporaryDirectory(prefix="audiocpp-anyformat-") as tmp:
        src = os.path.join(tmp, "input" + os.path.splitext(label)[1])
        dst = os.path.join(tmp, "converted.wav")
        with open(src, "wb") as fh:
            fh.write(data)
        cmd = [
            ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
            "-i", src,
            "-vn",                      # drop any video track (3GP may carry one)
            "-map_metadata", "-1",
            "-c:a", "pcm_s16le",
            "-f", "wav", dst,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except FileNotFoundError:
            raise ConversionError(
                f"ffmpeg executable not found ({ffmpeg_bin!r}). "
                "Install ffmpeg and put it on PATH, or pass --ffmpeg <path>.")
        except subprocess.TimeoutExpired:
            raise ConversionError(f"ffmpeg timed out converting {label!r}")
        if proc.returncode != 0:
            raise ConversionError(
                f"ffmpeg failed for {label!r} (exit {proc.returncode}): "
                f"{(proc.stderr or '').strip()[:500]}")
        with open(dst, "rb") as fh:
            out = fh.read()
    if not is_wav_bytes(out):
        raise ConversionError(f"converted output for {label!r} is not WAV")
    return out


# --------------------------------------------------------------------------
# Minimal multipart codec (stdlib only)
# --------------------------------------------------------------------------

def parse_multipart(body: bytes, boundary: str):
    """Split a multipart body into (headers_dict, payload_bytes) parts."""
    delim = b"--" + boundary.encode("ascii", "replace")
    parts = []
    for chunk in body.split(delim):
        if not chunk or chunk == b"--" or chunk == b"--\r\n":
            continue
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        if chunk.startswith(b"--"):  # closing marker remnant
            continue
        head, sep, payload = chunk.partition(b"\r\n\r\n")
        if not sep:
            continue
        payload = payload[:-2] if payload.endswith(b"\r\n") else payload
        msg = email.parser.Parser().parsestr(head.decode("utf-8", "replace"))
        headers = {k.lower(): v for k, v in msg.items()}
        parts.append((headers, payload))
    return parts


def part_filename(headers: dict) -> str:
    disp = headers.get("content-disposition", "")
    for piece in disp.split(";"):
        piece = piece.strip()
        if piece.lower().startswith("filename="):
            value = piece.split("=", 1)[1].strip()
            return value.strip('"')
    return ""


def part_name(headers: dict) -> str:
    disp = headers.get("content-disposition", "")
    for piece in disp.split(";"):
        piece = piece.strip()
        if piece.lower().startswith("name="):
            value = piece.split("=", 1)[1].strip()
            return value.strip('"')
    return ""


def rebuild_multipart(parts, boundary: str) -> bytes:
    out = bytearray()
    for headers, payload in parts:
        out += b"--" + boundary.encode("ascii") + b"\r\n"
        out += headers.encode("utf-8", "replace") + b"\r\n\r\n"
        out += payload + b"\r\n"
    out += b"--" + boundary.encode("ascii") + b"--\r\n"
    return bytes(out)


def content_disposition(name: str, filename: str | None) -> str:
    disp = f'form-data; name="{name}"'
    if filename:
        disp += f'; filename="{filename}"'
    return disp


# --------------------------------------------------------------------------
# Request rewriting
# --------------------------------------------------------------------------

class Converter:
    """Shared transcoding helper with per-request temp-file bookkeeping."""

    def __init__(self, ffmpeg_bin: str):
        self.ffmpeg_bin = ffmpeg_bin
        self._lock = threading.Lock()
        self._temp_files: list[str] = []

    def convert_bytes(self, data: bytes, label: str) -> bytes:
        return transcode_to_wav(data, label, self.ffmpeg_bin)

    def convert_file(self, path: str) -> str:
        """Convert an on-disk file to a sibling .wav; returns the new path."""
        with open(path, "rb") as fh:
            data = fh.read()
        if is_wav_bytes(data):
            return path
        out = self.convert_bytes(data, os.path.basename(path))
        stem = os.path.splitext(path)[0] or os.path.basename(path)
        dst = stem + ".wav"
        # Avoid clobbering an existing different file.
        if os.path.abspath(dst) != os.path.abspath(path):
            with open(dst, "wb") as fh:
                fh.write(out)
            with self._lock:
                self._temp_files.append(dst)
            return dst
        return path

    def cleanup(self):
        with self._lock:
            for path in self._temp_files:
                try:
                    os.unlink(path)
                except OSError:
                    pass
            self._temp_files.clear()


def rewrite_json_body(body_text: str, converter: Converter) -> tuple[str, bool]:
    """Transcode any server-local audio paths / base64 voice refs in a JSON body."""
    try:
        doc = json.loads(body_text)
    except ValueError:
        return body_text, False

    changed = False

    def rewrite_value(value):
        nonlocal changed
        if isinstance(value, str):
            # A server-local path that exists and holds non-WAV audio.
            # Sniff the head so unknown extensions (or a WAV misnamed
            # .3gpp) are handled by content, not just by extension.
            if os.path.isfile(value) and os.path.splitext(value)[1].lower() != ".wav":
                try:
                    with open(value, "rb") as fh:
                        head = fh.read(64)
                except OSError:
                    return value
                if looks_like_audio_file(value, head):
                    changed = True
                    return converter.convert_file(value)
            return value
        if isinstance(value, dict):
            # voice_ref: {"type": "path", "path": "..."} or
            #            {"type": "base64", "data": "..."}
            if value.get("type") == "path" and isinstance(value.get("path"), str):
                new_path = rewrite_value(value["path"])
                if new_path != value["path"]:
                    value["path"] = new_path
                    changed = True
            elif value.get("type") == "base64" and isinstance(value.get("data"), str):
                raw = value["data"]
                prefix = ""
                if raw.startswith("data:"):
                    head, sep, raw = raw.partition(",")
                    prefix = head + "," if sep else ""
                try:
                    decoded = base64.b64decode(raw)
                except Exception:
                    return value
                if not is_wav_bytes(decoded) and sniff_container(decoded):
                    wav = converter.convert_bytes(decoded, "voice_ref")
                    encoded = base64.b64encode(wav).decode("ascii")
                    if len(encoded) > 5 * 1024 * 1024:
                        raise ConversionError(
                            "converted voice_ref base64 exceeds the 5 MiB server limit")
                    value["data"] = prefix + encoded
                    changed = True
            else:
                for key in list(value):
                    value[key] = rewrite_value(value[key])
            return value
        if isinstance(value, list):
            return [rewrite_value(entry) for entry in value]
        return value

    # Generic recursive walk: AUDIO_PATH_KEYS and voice_ref are the
    # audio-bearing fields; other string values only change when they
    # point at an existing file that sniffs as non-WAV audio, which
    # non-audio fields (models, prompts, ...) never do.
    if isinstance(doc, dict):
        for key in list(doc):
            doc[key] = rewrite_value(doc[key])
    if not changed:
        return body_text, False
    return json.dumps(doc), True


def rewrite_multipart_body(body: bytes, boundary: str, converter: Converter) -> tuple[bytes, str]:
    """Transcode every non-WAV file part; returns (new_body, new_boundary)."""
    parts = parse_multipart(body, boundary)
    new_boundary = "audiocpp-anyformat-" + os.urandom(16).hex()
    rebuilt: list[tuple[str, bytes]] = []  # (header_block, payload)
    for headers, payload in parts:
        name = part_name(headers)
        filename = part_filename(headers)
        ctype = headers.get("content-type", "")
        if filename and looks_like_audio_file(filename, payload):
            wav = converter.convert_bytes(payload, filename)
            payload = wav
            stem = os.path.splitext(filename)[0] or "upload"
            filename = stem + ".wav"
            ctype = "audio/wav"
            headers = {
                k: v for k, v in headers.items()
                if k not in ("content-type", "content-disposition")
            }
            headers["content-disposition"] = content_disposition(name, filename)
            if ctype:
                headers["content-type"] = ctype
        head = "\r\n".join(f"{k}: {v}" for k, v in headers.items())
        rebuilt.append((head, payload))
    out = bytearray()
    for head, payload in rebuilt:
        out += b"--" + new_boundary.encode() + b"\r\n"
        out += head.encode() + b"\r\n\r\n"
        out += payload + b"\r\n"
    closing = b"--" + new_boundary.encode() + b"--\r\n"
    out += closing
    return bytes(out), new_boundary


# --------------------------------------------------------------------------
# HTTP plumbing
# --------------------------------------------------------------------------

class ProxyState:
    def __init__(self, backend_host: str, backend_port: int, ffmpeg_bin: str):
        self.backend_host = backend_host
        self.backend_port = backend_port
        self.converter = Converter(ffmpeg_bin)
        self.stats = {"requests": 0, "conversions": 0, "errors": 0}
        self.stats_lock = threading.Lock()

    def bump(self, key: str):
        with self.stats_lock:
            self.stats[key] += 1


def forward_request(state: ProxyState, method: str, path: str, headers: dict, body: bytes):
    conn = http.client.HTTPConnection(state.backend_host, state.backend_port, timeout=3600)
    fwd_headers = {k: v for k, v in headers.items()
                   if k.lower() not in HOP_BY_HOP_HEADERS}
    if os.environ.get("ANYFORMAT_DEBUG"):
        print("[proxy] fwd", method, path, dict(fwd_headers), file=sys.stderr)
    fwd_headers["Host"] = f"{state.backend_host}:{state.backend_port}"
    fwd_headers["X-Forwarded-By"] = "audiocpp-anyformat-proxy"
    if body:
        fwd_headers["Content-Length"] = str(len(body))
        conn.request(method, path, body=body, headers=fwd_headers)
    else:
        conn.request(method, path, headers=fwd_headers)
    return conn, conn.getresponse()


def send_response(client, status: int, headers: list[tuple[str, str]], body: bytes):
    client.send_response(status)
    for key, value in headers:
        if key.lower() not in HOP_BY_HOP_HEADERS:
            client.send_header(key, value)
    client.send_header("Content-Length", str(len(body)))
    client.end_headers()
    if body and client.command != "HEAD":
        client.wfile.write(body)


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "audiocpp-anyformat-proxy/0.1"
    state: ProxyState = None  # set by make_server

    def log_message(self, fmt, *args):
        sys.stderr.write("[proxy] " + (fmt % args) + "\n")

    # -- helpers ----------------------------------------------------------

    def read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length > MAX_UPLOAD_BYTES:
            raise ConversionError("request body exceeds the 2 GiB server limit")
        return self.rfile.read(length) if length else b""

    def do_GET(self):
        self.proxy_plain()

    def do_HEAD(self):
        self.proxy_plain()

    def do_DELETE(self):
        self.proxy_plain()

    def do_POST(self):
        state = self.state
        state.bump("requests")
        path = urllib.parse.urlparse(self.path).path
        ctype = self.headers.get("Content-Type", "")
        body = self.read_body()

        try:
            # 1. WebUI staging endpoint: the browser's "open file" dialog
            #    lands here first. Convert anything non-WAV.
            if path == "/v1/ui/upload":
                filename = self.headers.get("X-AudioCPP-Filename", "audio.bin")
                overrides = {}
                if looks_like_audio_file(filename, body):
                    body = state.converter.convert_bytes(body, filename)
                    # email.message.Message.__setitem__ appends duplicate
                    # headers instead of replacing, so override explicitly.
                    overrides["X-AudioCPP-Filename"] = "upload.wav"
                    state.bump("conversions")
                    self.log_message("converted staged upload %s -> WAV (%d bytes)",
                                     filename, len(body))
                return self.forward_now(ctype, body, overrides)

            # 2. Multipart API routes (OpenAI-style clients, batch ASR).
            if path in MULTIPART_ROUTES and ctype.startswith("multipart/form-data"):
                boundary = extract_boundary(ctype)
                if boundary:
                    new_body, new_boundary = rewrite_multipart_body(body, boundary, state.converter)
                    if new_boundary != boundary:
                        state.bump("conversions")
                        self.log_message("converted multipart upload on %s", path)
                        ctype = f"multipart/form-data; boundary={new_boundary}"
                    return self.forward_now(ctype, new_body)

            # 3. JSON API routes with server-local audio paths / voice refs.
            if path in JSON_SCAN_ROUTES and "json" in ctype.lower():
                new_body, changed = rewrite_json_body(body.decode("utf-8"), state.converter)
                if changed:
                    state.bump("conversions")
                    self.log_message("rewrote audio path(s) in JSON %s", path)
                return self.forward_now(ctype, new_body.encode("utf-8"))

            # 4. Everything else: blind pass-through.
            return self.forward_now(ctype, body)
        except ConversionError as exc:
            state.bump("errors")
            self.log_message("conversion failed: %s", exc)
            payload = json.dumps({
                "error": {
                    "message": f"audiocpp-anyformat proxy: {exc}",
                    "type": "invalid_request_error",
                    "hint": ("install an ffmpeg build with AMR support to decode "
                             ".3gpp/.3gp/.amr; check with `ffmpeg -decoders | findstr amr`"),
                }
            }).encode("utf-8")
            send_response(self, 415, [("Content-Type", "application/json")], payload)
        except Exception:
            state.bump("errors")
            self.log_message("internal error:\n%s", traceback.format_exc())
            payload = json.dumps({"error": {"message": "proxy internal error"}}).encode()
            send_response(self, 502, [("Content-Type", "application/json")], payload)

    # -- forwarding -------------------------------------------------------

    def forward_now(self, ctype: str, body: bytes, overrides: dict | None = None):
        seen = set()
        headers = {}
        for k, v in self.headers.items():
            if k.lower() in HOP_BY_HOP_HEADERS:
                continue
            # Keep the first occurrence of each header (email.message
            # may hold duplicates after mutation).
            if k.lower() in seen:
                continue
            seen.add(k.lower())
            headers[k] = v
        if overrides:
            # Drop case-variants of overridden keys: http.server
            # normalizes incoming header case, so a naive dict update
            # would send both the old and the new header.
            drop = {k.lower() for k in overrides}
            headers = {k: v for k, v in headers.items()
                       if k.lower() not in drop}
            headers.update(overrides)
        if body:
            headers["Content-Length"] = str(len(body))
        if ctype:
            headers["Content-Type"] = ctype
        conn, resp = forward_request(
            self.state, self.command, self.path, headers, body)
        try:
            resp_body = resp.read()
            send_response(self, resp.status, list(resp.getheaders()), resp_body)
        finally:
            conn.close()

    def proxy_plain(self):
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in HOP_BY_HOP_HEADERS}
        conn, resp = forward_request(
            self.state, self.command, self.path, headers, b"")
        try:
            resp_body = resp.read()
            send_response(self, resp.status, list(resp.getheaders()), resp_body)
        finally:
            conn.close()


def extract_boundary(ctype: str) -> str | None:
    for piece in ctype.split(";"):
        piece = piece.strip()
        if piece.lower().startswith("boundary="):
            return piece.split("=", 1)[1].strip('"')
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--backend", default="http://127.0.0.1:8081",
                        help="audiocpp_server base URL (default http://127.0.0.1:8081)")
    parser.add_argument("--listen", default="127.0.0.1:8080",
                        help="address:port for this proxy (default 127.0.0.1:8080)")
    parser.add_argument("--ffmpeg", default="ffmpeg",
                        help="ffmpeg executable (default: ffmpeg on PATH)")
    parser.add_argument("--ext", action="append", default=[],
                        help="extra extension to convert, e.g. --ext .mp4 (repeatable)")
    args = parser.parse_args()

    global FFMPEG_BIN
    FFMPEG_BIN = args.ffmpeg
    for extra in args.ext:
        TRANSCODE_EXTENSIONS.add(extra.lower())

    parsed = urllib.parse.urlparse(args.backend)
    backend_host = parsed.hostname or "127.0.0.1"
    backend_port = parsed.port or 8081

    if not shutil.which(args.ffmpeg):
        print(f"WARNING: ffmpeg {args.ffmpeg!r} not found on PATH; "
              "non-WAV uploads will be rejected.", file=sys.stderr)
    elif not ffmpeg_check():
        print("WARNING: ffmpeg found but it cannot decode AMR-NB; "
              ".3gpp/.3gp/.amr will fail (use a 'full' ffmpeg build).",
              file=sys.stderr)

    state = ProxyState(backend_host, backend_port, args.ffmpeg)
    ProxyHandler.state = state

    listen_host, _, listen_port = args.listen.rpartition(":")
    if listen_host.startswith("["):  # ipv6 literal
        listen_host = listen_host.strip("[]")
    server = ThreadingHTTPServer((listen_host or "127.0.0.1", int(listen_port)), ProxyHandler)
    print(f"audiocpp-anyformat proxy listening on http://{args.listen}")
    print(f"forwarding to audiocpp_server at {args.backend}")
    print("open the WebUI via the proxy URL and pick .3gpp / .amr / .mp3 / ... files")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        state.converter.cleanup()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
