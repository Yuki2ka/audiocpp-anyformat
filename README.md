# TODO

**Bonus path (if you later want it upstream):** audio.cpp has an official plugin point — `external/audio.cpp-server-frontends` (the `audio_decode` module already rewrites MP3/FLAC→WAV in `pre_process`). Extending it with an ffmpeg-backed decoder needs only a module compile (core untouched) and would work for *every* API client, not just the WebUI. CONTRIBUTING.md explicitly invites API-server improvements.

# audiocpp-anyformat

Make **any audio format** openable in [audio.cpp](https://github.com/0xShug0/audio.cpp)
— including `.3gpp` voice notes from old Android phones — **without
recompiling the C++ server**.

audio.cpp's core only parses WAV (`src/framework/audio/wav_reader.cpp`)
and its multipart routes reject non-`.wav` filenames
(`app/server/server_protocol.cpp`). This project adds the missing
formats in front of the server, two ways:

| Approach | Recompiles C++? | Effort | Coverage |
|---|---|---|---|
| **Python transcode proxy** (`proxy/`) | ❌ no | run one process | WebUI dialog + all API routes |
| Server frontend module | module only | ~1 file + relink | all API clients (upstream-able) |

See [`docs/input-formats-investigation.md`](docs/input-formats-investigation.md)
for the full investigation: the four format gates, why renaming
`.3gpp`→`.wav` cannot work (content is sniffed), why a pure
HTML/JS fix requires rebuilding the embedded WebUI, and the
trade-offs of each option.

## Quick start (Python proxy, zero C++ changes)

```powershell
# 1. audio.cpp on a private port
audiocpp_server --ui --ui-management --backend cuda --port 8081

# 2. this proxy in front (needs ffmpeg on PATH)
python proxy\audiocpp_anyformat_proxy.py --backend http://127.0.0.1:8081
#    or:  run_proxy.cmd

# 3. open http://127.0.0.1:8080 and pick .3gpp / .3gp / .amr /
#    .mp3 / .m4a / .ogg / .flac / ... in every file dialog
```

Linux/macOS: `./run_proxy.sh`.

The proxy transcodes to PCM16 WAV with ffmpeg before the C++
server sees anything; source sample rate/channels are preserved
(AMR-NB is 8 kHz mono) and the models resample internally as
usual. WAV uploads pass through untouched.

### Requirements

* Python 3.8+ (standard library only)
* `ffmpeg` — for `.3gpp`/`.3gp`/`.amr` it must include AMR
  (check: `ffmpeg -hide_banner -decoders | findstr amr`; the
  gyan.dev / BtbN "full" Windows builds do)

### Tests

```bash
python3 tests/run_proxy_tests.py
```

Generates a real AMR-NB `.3gpp`, runs the proxy against a stub
that mimics audio.cpp's gates, and exercises the staged-upload,
multipart, JSON-path, and WAV-passthrough shapes.
