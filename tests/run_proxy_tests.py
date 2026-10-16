#!/usr/bin/env python3
"""End-to-end test for the anyformat proxy against the stub server.

Creates real audio samples (incl. a .3gpp with AMR-NB), runs the proxy
and the stub backend, then exercises every route shape.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "proxy"))

try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except ImportError:
    FFMPEG = shutil.which("ffmpeg")
    if not FFMPEG:
        sys.exit("no ffmpeg available")

PROXY_PORT = 8090
STUB_PORT = 8091
TMP = tempfile.mkdtemp(prefix="anyformat-test-")


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def make_samples():
    """sine.wav -> sine.3gpp (AMR-NB), sine.mp3, keep sine.wav."""
    wav = os.path.join(TMP, "sine.wav")
    assert run([FFMPEG, "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                "-ar", "8000", "-ac", "1", "-c:a", "pcm_s16le", wav]).returncode == 0
    g3pp = os.path.join(TMP, "voice_note.3gpp")
    rc = run([FFMPEG, "-y", "-loglevel", "error", "-i", wav,
              "-c:a", "libopencore_amrnb", "-f", "3gp", g3pp])
    if rc.returncode != 0:  # fallback muxer name
        rc = run([FFMPEG, "-y", "-loglevel", "error", "-i", wav,
                  "-c:a", "libopencore_amrnb", g3pp])
    assert rc.returncode == 0, rc.stderr
    mp3 = os.path.join(TMP, "song.mp3")
    assert run([FFMPEG, "-y", "-loglevel", "error", "-i", wav, "-c:a", "libmp3lame", mp3]).returncode == 0
    return wav, g3pp, mp3


def start(script, *args):
    proc = subprocess.Popen([sys.executable, script, *args],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    return proc


def http_json(method, url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def multipart_body(fields, file_field, filename, file_bytes, file_ctype):
    boundary = "test-boundary-1234"
    out = b""
    for name, value in fields.items():
        out += (f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n').encode()
    out += (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
            f"Content-Type: {file_ctype}\r\n\r\n").encode()
    out += file_bytes + f"\r\n--{boundary}--\r\n".encode()
    return boundary, out


def main():
    wav, g3pp, mp3 = make_samples()
    print(f"samples in {TMP}: {os.path.getsize(g3pp)} byte .3gpp created")

    stub = start(os.path.join(HERE, "stub_audiocpp_server.py"), str(STUB_PORT))
    proxy = start(os.path.join(ROOT, "proxy", "audiocpp_anyformat_proxy.py"),
                  "--backend", f"http://127.0.0.1:{STUB_PORT}",
                  "--listen", f"127.0.0.1:{PROXY_PORT}",
                  "--ffmpeg", FFMPEG)
    base = f"http://127.0.0.1:{PROXY_PORT}"
    failures = []

    def wait_up(url, proc):
        for _ in range(50):
            if proc.poll() is not None:
                sys.exit(f"process died:\n{proc.stdout.read() if proc.stdout else ''}")
            try:
                urllib.request.urlopen(url, timeout=2)
                return
            except Exception:
                time.sleep(0.2)
        sys.exit("server did not start")

    try:
        wait_up(base + "/", proxy)
        wait_up(f"http://127.0.0.1:{STUB_PORT}/", stub)

        # -- 1. WebUI staging path: upload .3gpp, then transcribe by path ----
        with open(g3pp, "rb") as fh:
            g3pp_bytes = fh.read()
        status, body = http_json("POST", base + "/v1/ui/upload", g3pp_bytes,
                                 {"Content-Type": "audio/3gpp",
                                  "X-AudioCPP-Filename": "upload.3gpp"})
        assert status == 200, (status, body)
        staged = json.loads(body)["path"]
        print("1. /v1/ui/upload .3gpp ->", staged)

        req = json.dumps({"model": "stub", "audio": staged}).encode()
        status, body = http_json("POST", base + "/v1/audio/transcriptions", req,
                                 {"Content-Type": "application/json"})
        print("   transcriptions (staged .3gpp):", status, body.decode()[:80])
        assert status == 200, (status, body)

        # -- 2. Multipart direct upload of .3gpp ----------------------------
        boundary, mbody = multipart_body(
            {"model": "stub"}, "file", "voice_note.3gpp", g3pp_bytes, "audio/3gpp")
        status, body = http_json("POST", base + "/v1/audio/transcriptions", mbody,
                                 {"Content-Type": f"multipart/form-data; boundary={boundary}"})
        print("2. multipart .3gpp ->", status, body.decode()[:80])
        assert status == 200, (status, body)

        # -- 3. Multipart .mp3 ---------------------------------------------
        with open(mp3, "rb") as fh:
            mp3_bytes = fh.read()
        boundary, mbody = multipart_body(
            {"model": "stub"}, "file", "song.mp3", mp3_bytes, "audio/mpeg")
        status, body = http_json("POST", base + "/v1/audio/transcriptions", mbody,
                                 {"Content-Type": f"multipart/form-data; boundary={boundary}"})
        print("3. multipart .mp3 ->", status, body.decode()[:80])
        assert status == 200, (status, body)

        # -- 4. JSON path to a .3gpp file ----------------------------------
        req = json.dumps({"model": "stub", "audio": g3pp}).encode()
        status, body = http_json("POST", base + "/v1/audio/transcriptions", req,
                                 {"Content-Type": "application/json"})
        print("4. json path .3gpp ->", status, body.decode()[:80])
        assert status == 200, (status, body)

        # -- 5. WAV must pass through untouched -----------------------------
        with open(wav, "rb") as fh:
            wav_bytes = fh.read()
        boundary, mbody = multipart_body(
            {"model": "stub"}, "file", "sine.wav", wav_bytes, "audio/wav")
        status, body = http_json("POST", base + "/v1/audio/transcriptions", mbody,
                                 {"Content-Type": f"multipart/form-data; boundary={boundary}"})
        print("5. multipart .wav passthrough ->", status, body.decode()[:80])
        assert status == 200, (status, body)

        # -- 6. Plain GET passthrough ---------------------------------------
        status, body = http_json("GET", base + "/", None)
        assert status == 200 and b"stub WebUI" in body
        print("6. GET / passthrough ok")

        # -- 7. Uppercase extension ------------------------------------------
        boundary, mbody = multipart_body(
            {"model": "stub"}, "file", "CLIP.3GPP", g3pp_bytes, "audio/3gpp")
        status, body = http_json("POST", base + "/v1/audio/transcriptions", mbody,
                                 {"Content-Type": f"multipart/form-data; boundary={boundary}"})
        print("7. multipart .3GPP (uppercase) ->", status)
        assert status == 200, (status, body)

        # -- 8. Unknown extension, sniffed as 3GP ----------------------------
        sniff_name = os.path.join(TMP, "mystery.bin")
        with open(sniff_name, "wb") as fh:
            fh.write(g3pp_bytes)
        req = json.dumps({"model": "stub", "audio": sniff_name}).encode()
        status, body = http_json("POST", base + "/v1/audio/transcriptions", req,
                                 {"Content-Type": "application/json"})
        print("8. sniffed 3gpp content (.bin) ->", status)
        assert status == 200, (status, body)

        # -- 9. Raw AMR file ---------------------------------------------------
        amr = os.path.join(TMP, "note.amr")
        assert run([FFMPEG, "-y", "-loglevel", "error", "-i", wav,
                    "-c:a", "libopencore_amrnb", amr]).returncode == 0
        with open(amr, "rb") as fh:
            amr_bytes = fh.read()
        status, body = http_json("POST", base + "/v1/ui/upload", amr_bytes,
                                 {"Content-Type": "audio/amr",
                                  "X-AudioCPP-Filename": "upload.amr"})
        assert status == 200, (status, body)
        staged_amr = json.loads(body)["path"]
        req = json.dumps({"model": "stub", "audio": staged_amr}).encode()
        status, body = http_json("POST", base + "/v1/audio/transcriptions", req,
                                 {"Content-Type": "application/json"})
        print("9. raw .amr via staging ->", status)
        assert status == 200, (status, body)

        # -- 10. WAV named .3gpp passes through without conversion -----------
        with open(wav, "rb") as fh:
            wav_bytes = fh.read()
        status, body = http_json("POST", base + "/v1/ui/upload", wav_bytes,
                                 {"Content-Type": "audio/wav",
                                  "X-AudioCPP-Filename": "upload.3gpp"})
        assert status == 200, (status, body)
        staged_wav = json.loads(body)["path"]
        req = json.dumps({"model": "stub", "audio": staged_wav}).encode()
        status, body = http_json("POST", base + "/v1/audio/transcriptions", req,
                                 {"Content-Type": "application/json"})
        print("10. WAV misnamed .3gpp -> passthrough", status)
        assert status == 200, (status, body)

        print("\nALL PROXY TESTS PASSED")
    except AssertionError as exc:
        failures.append(exc)
        print("\nFAIL:", exc)
        for name, proc in (("proxy", proxy), ("stub", stub)):
            try:
                proc.terminate()
                out, _ = proc.communicate(timeout=5)
                print(f"--- {name} log ---\n{out}")
            except Exception:
                pass
        sys.exit(1)
    finally:
        proxy.terminate()
        stub.terminate()
        shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
