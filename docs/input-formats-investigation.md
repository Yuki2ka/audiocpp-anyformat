# Investigation: more input formats for audio.cpp (without recompiling C++)

Goal: open **more audio formats in the WebUI file dialog** — especially
`.3gpp` voice notes from old Android phones — ideally **without
recompiling the C++ part**, from the Python or HTML/JS side. Windows +
ffmpeg are available.

Repo investigated: [`0xShug0/audio.cpp`](https://github.com/0xShug0/audio.cpp)
(v0.9.x, commit `2cb4df2` lineage).

---

## 1. Where formats are gated today (the full chain)

A file picked in the WebUI dialog travels through **four gates**:

| # | Gate | Location | Rule |
|---|------|----------|------|
| 1 | Browser file dialog | `webui/native/src/routes/+page.svelte` etc. | `<input type="file" accept="audio/*">` — **`.3gpp` already shows up** (MIME `audio/3gpp` matches `audio/*` on Windows/Chrome/Edge; if a machine lacks the MIME registration, "All files" works) |
| 2 | WebUI staging upload | `POST /v1/ui/upload` → `app/server/server_ui.cpp: ui_upload_response()` | Writes bytes verbatim to a temp file (`upload.3gpp`), **no content check** — staging succeeds |
| 3 | Multipart API routes | `app/server/server_protocol.cpp:694 is_wav_upload_filename()` | Accepts only **empty extension or `.wav`**; everything else → HTTP 400 *"only WAV audio uploads are currently supported for transcription"* |
| 4 | Core decoder | `src/framework/audio/wav_reader.cpp read_wav_f32()` | **WAV only** (PCM/float/A-law/μ-law/WAVEFORMATEXTENSIBLE). Foreign containers are sniffed (FLAC, Ogg, MP3, MP4/M4A, AIFF, RF64, CAF, Matroska) and rejected with: *"input is MP4/M4A (AAC or ALAC), not WAV; convert it first, e.g. `ffmpeg -i input ...`"* |

A `.3gpp` file is an ISO-BMFF container (`ftyp` brand at byte 4) holding
**AMR-NB** audio (8 kHz mono) — so it fails gate 4 with the misleading
"MP4/M4A" message (gate 3 only applies to direct multipart API clients,
not the WebUI's staging flow).

**Key consequence:** renaming `.3gpp` → `.wav` does **not** work — the
content is sniffed (gate 4). The bytes must actually become WAV.

Other audio-ingest routes with the same gates: `/v1/audio/transcriptions`
(+`/details`), `/v1/batches/transcriptions`, `/v1/audio/alignments`
(multipart `file` parts, gate 3+4), and JSON routes that take
server-local paths (`audio` / `audio_path` / `file` fields, gate 4).
TTS voice references use `voice_ref` (`{"type":"path","path":...}` or
`{"type":"base64","data":...}`, 5 MiB cap).

## 2. Escape hatches that already exist upstream (no code changes)

1. **CLI workflows** — `app/workflow/workflow.cpp: run_convert_audio_step()`
   shells out to a converter (`--audio-converter <cmd>`, default `ffmpeg`)
   for a `convert_audio` JSON-pipeline step. Batch/CLI only, not the dialog.
2. **Server frontend modules** — `external/audio.cpp-server-frontends`
   (separate git repo, opt-in at configure time):
   `-DAUDIOCPP_BUILD_SERVER_FRONTENDS=ON -DAUDIOCPP_SERVER_FRONTEND_MODULES=audio_decode`.
   The `audio_decode` module rewrites MP3/FLAC → temp WAV in
   `pre_process()` **before** the core handler runs. It uses miniaudio,
   which **cannot decode AMR**, so `.3gpp` needs ffmpeg/libav there.
3. **`--audio-converter` CLI flag** — same idea for workflow imports.

## 3. Options ranked for "no C++ recompile"

### ✅ Option A — Python transcode reverse-proxy (recommended, prototype included)

`proxy/audiocpp_anyformat_proxy.py` (stdlib-only Python 3.8+):

```
browser ──(any format)──> proxy :8080 ──(WAV only)──> audiocpp_server :8081
```

* Intercepts `POST /v1/ui/upload` (the dialog's staging endpoint),
  multipart `file` parts, and JSON audio paths / `voice_ref` values;
  transcodes non-WAV payloads to PCM16 WAV with **ffmpeg** and forwards.
* Everything else (UI pages, model management, TTS output, SSE streams)
  is proxied untouched. The WebUI uses same-origin relative URLs
  (`apiUrl()` in `webui/native/src/lib/api.ts`), so pointing the browser
  at the proxy port just works — no CORS, no UI rebuild.
* Keeps source sample rate/channels (AMR-NB = 8 kHz mono); the C++ model
  frontends resample internally exactly as they do for WAV input.
* Windows-friendly: `python proxy\audiocpp_anyformat_proxy.py --backend
  http://127.0.0.1:8081 [--ffmpeg C:\ffmpeg\bin\ffmpeg.exe]`.
* **Zero audio.cpp changes, zero recompile.** New formats = add an
  extension to `TRANSCODE_EXTENSIONS` (ffmpeg does the rest).
* Trade-offs: one extra process to run; converted files are temp/cleaned;
  ffmpeg must have AMR support for `.3gpp` (see §5).

Tested end-to-end (`tests/run_proxy_tests.py`): real AMR-NB `.3gpp`
generated with ffmpeg, converted and accepted through all four request
shapes (staged upload, multipart, JSON path, WAV passthrough).

### ⚙️ Option B — Server frontend module (official extension point, small C++ compile)

Extend `external/audio.cpp-server-frontends/modules/audio_decode.cpp`
(or add `any_decode.cpp` beside it):

* add `.3gpp/.3gp/.amr/...` to `format_from_extension()` + sniff the
  `ftyp`/`#!AMR` magics in `sniff_format()`;
* decode by spawning ffmpeg (pattern already exists in
  `app/workflow/workflow.cpp`) or dlopening `libavformat` (pattern in
  `src/models/controlfoley/video_processing.cpp`);
* register in `server_frontends.cmake`; build the server with
  `-DAUDIOCPP_BUILD_SERVER_FRONTENDS=ON`.

The **core repo stays untouched** and the module is compiled separately,
but the server binary is still relinked — so it does not meet the
"no recompile" constraint. It is the right long-term / upstream-PR
shape (works for every API client, not just this proxy's routes), and
`frontend_options` in `server.json` can configure the ffmpeg path.

### ❌ Option C — HTML/JS side only (browser transcode): not viable without a rebuild

* The WebUI is a SvelteKit bundle **embedded into the server binary**
  (`webui/native/dist/index.html` → generated header →
  `app/server/ui_assets.cpp: embedded_ui_html()`). There is no
  serve-UI-from-disk flag, so any JS change requires regenerating the
  embedded header **and recompiling the server**.
* The UI is served with `Content-Security-Policy: default-src 'self' …`
  (`app/server/server_ui.cpp: ui_asset_response()`), so loading
  ffmpeg.wasm from a CDN is blocked; it would have to be vendored into
  the bundle anyway.
* Technically possible if you fork+rebuild regardless: transcode in the
  browser with ffmpeg.wasm before `uploadFile()` — but AMR support
  depends on the ffmpeg.wasm build, and a pure-JS AMR-NB decoder +
  3GP box parser would be lighter. Not needed for Option A.

### 🔁 Option D — CLI/workflow path (works today, batch only)

`audiocpp_cli ... --audio-converter ffmpeg` with a JSON workflow whose
first step is `convert_audio` (see `app/workflow/workflow.cpp`). Good
for bulk old-phone recordings; does not help the WebUI dialog.

### 🐍 Option E — Python client-side wrapper

If you drive the server from your own Python code, transcode with
`subprocess.run(["ffmpeg", "-y", "-i", src, "-c:a", "pcm_s16le", dst])`
before POSTing. Trivial, but doesn't fix the WebUI dialog.

## 4. Recommendation

* **Now / no recompile:** Option A — run the proxy in front. `.3gpp`,
  `.3gp`, `.amr`, `.mp3`, `.m4a`, `.ogg`, `.opus`, `.flac`, `.wma`,
  `.aiff`, `.caf`, `.mkv`, `.webm`, `.mov` … all become openable in the
  dialog (anything ffmpeg decodes).
* **Long-term / upstream:** Option B — contribute an ffmpeg-backed
  `audio_decode` extension to `audio.cpp-server-frontends` (the project's
  CONTRIBUTING.md explicitly welcomes API-server improvements). It would
  make every client (Open WebUI, Whisper-style tools) format-agnostic.

## 5. ffmpeg AMR note (Windows)

`.3gpp`/`.amr` need the AMR-NB decoder. Verify:

```powershell
ffmpeg -hide_banner -decoders | findstr amr
```

You want lines like `amrnb` / `libopencore_amrnb`. The gyan.dev and
BtbN "full"/"essentials" Windows builds include them; if yours doesn't,
grabbing a full build fixes it. The proxy logs a clear 415 error if
ffmpeg is missing or lacks AMR, and still passes WAV through untouched.

## 6. Quick start (Option A)

```powershell
# 1. audio.cpp on a private port
audiocpp_server --ui --ui-management --backend cuda --port 8081

# 2. proxy in front (default ffmpeg on PATH)
python proxy\audiocpp_anyformat_proxy.py --backend http://127.0.0.1:8081

# 3. open http://127.0.0.1:8080 and pick your .3gpp voice notes
```

Or use `run_proxy.cmd` (Windows) / `run_proxy.sh` (Linux/macOS).
