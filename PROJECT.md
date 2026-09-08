# PROJECT.md — webcam-guard

> This is the **handover document for whoever works on this next** (AI sessions included).
> `README.md` is the end-user install guide; this file carries design intent, verification status, and what to watch out for when changing things.
> This file holds **the conclusions**; [CHANGELOG.md](CHANGELOG.md) holds **how they were reached** and which attempts were reversed.
> **Read this end to end before changing code**, especially "Do not touch".

*[한국어는 아래에 ↓](#한국어)*

---

## 1. Overview

A spare USB webcam turned into a camera that watches a room while nobody is home.

| Item | Decision |
|---|---|
| Environment | **A Windows PC** left running |
| Approach | **Written by hand** — Frigate, motionEye and similar are deliberately not used |
| Required | ① continuous recording ② live view from a phone ③ automatic deletion of old footage ④ a motion/face logbook |
| Excluded | audio, alerts/push, face recognition that **identifies** people, cloud upload |
| Dependencies | ffmpeg (an external binary) plus the Python standard library. **Zero pip packages.** OpenCV is used for faces if present and skipped if not |

④ was added later. The reason is that over long runs **nothing happens most of the time**; continuous recording had to stay, but it needed an index pointing at the parts worth looking at. Reasoning in 5-12 through 5-15.

---

## 2. Current state — what is verified and what is not

**This distinction matters more than anything else here.** Do not build on an "unverified" item as though it works.

### ✅ Verified on real Windows with a real webcam (2026-08-23/24)

Environment: Windows 11 Pro 26200 · Python 3.12.8 · **ffmpeg 9.0** (Gyan build, winget) · a Logitech C920.

- **dshow input** — recording plus simultaneous HLS output, on a real camera. Both raw (yuyv422) and mjpeg measured at 720p 15fps.
- **drawtext timestamp** — the clock visibly burnt into the picture. **Only after fixing the escaping** (5-8).
- **Browser HLS playback** — live tab in Chromium, plus the archive tab's 24-hour timeline and clip playback.
- **Offline operation** — **zero** external requests when the viewer loads (assets served from `static/`).
- `tee` muxer writing segment (mp4) and HLS at once / every HTTP route / Basic auth / Range 206 / path traversal 404.
- **Live-file race** — reproduced under 12 concurrent threads for 45 seconds and fixed (5-11). Afterwards: zero server exceptions, 78242 keep-alive requests all clean.
- Measured storage: **15.4 GB per day** at 720p·15fps·1500k (26.3 GB at 1080p·15fps·2500k).
- **Logbook** — an end-to-end run wrote a face event and `/api/events` resolved it to the containing clip and offset.

**All of it is reproducible with one line: `python tools/selftest.py`. 54 items pass.** Do not hand-run curl.

### ❌ Not verified yet

1. **Long-run stability** — more than 24 hours continuously, the janitor actually deleting (over size and over age), the watchdog actually restarting. The longest continuous run so far is minutes.
2. **iOS Safari native HLS** — the code path exists but no real device has been tried (only Chromium).
3. **Concurrent viewers** — the live playlist was hammered with 12 threads (5-11), but not several people actually *playing video* (segment and clip bandwidth).

> **First thing for the next session**: item 1. There is no shortcut other than leaving it running for a day and watching `restarts` and `used_gb` in `/api/status`.

---

## 3. Architecture

```
        ┌──────────────────────── guard.py (Python, one process) ──────────┐
        │                                                                  │
webcam  │  [supervisor thread] ── subprocess ──> ffmpeg.exe                │
  │     │        restart + backoff                 │                       │
  └─────┼───────────────────────────────────────────┤ tee muxer            │
        │                                           │                       │
        │                        ┌──────────────────┴──────────────────┐   │
        │                        ▼                                     ▼   │
        │           clips/YYYY-MM-DD_HH-MM-SS.mp4          live/live.m3u8  │
        │           (10-minute archive)                     live/segN.ts    │
        │                        │                             │        │   │
        │  [janitor thread] ─────┘ deletes over size/age       │        │   │
        │                                                      │        │   │
        │  [analyzer thread] ─────────── reads finished segments ┘       │   │
        │        │  motion (% changed pixels) + faces (OpenCV, optional) │   │
        │        └──> events/YYYY-MM-DD.jsonl  (merged into intervals)   │   │
        │                                                              │   │
        │  [ThreadingHTTPServer :8088] ── Basic Auth ──────────────────┘   │
        │        └─> index.html (viewer) + JSON API + file serving          │
        └──────────────────────────────────────────────────────────────────┘
                                   ▲
                        phone browser (via Tailscale)
```

### Why this shape — the core constraint

**On Windows only one process at a time can hold a webcam.**
So "one ffmpeg for recording plus one for streaming" is **impossible**. Exactly one ffmpeg process must own the camera and split its output with the `tee` muxer. Encoding then happens once, which also saves CPU.

Consequences that follow automatically:

- A new feature that **needs the camera** must go inside the existing pipeline (a `select` filter, or a third tee output).
- Conversely, **reading files that were already written is free.** That is exactly how the logbook works (5-12).
- Any code that tries to open the camera a second time breaks immediately.

---

## 4. Files

```
webcam-guard\                  # the repo. It can be deployed anywhere
  ├─ guard.py                  # the entire server (~700 lines, one file)
  ├─ index.html                # phone viewer (CSS/JS inline, assets from static/)
  ├─ .env.example              # settings template. Copy to .env (which is gitignored)
  ├─ static\                   # vendored assets, fetched by tools/vendor.py
  │   ├─ hls.min.js            #   hls.js 1.5.13 (Apache-2.0)
  │   ├─ fonts.css             #   @font-face, generated by vendor.py — do not edit
  │   └─ ibm-plex-*.woff2      #   IBM Plex latin subset (OFL-1.1)
  ├─ README.md                 # user-facing install and operation guide
  ├─ PROJECT.md                # this file
  ├─ CHANGELOG.md              # what changed and why, reversed decisions included
  ├─ CLAUDE.md                 # agent summary + absolute rules (this file wins)
  ├─ .gitignore                # blocks recordings and password-bearing files
  ├─ .editorconfig             # 4-space / LF, CRLF for .bat
  ├─ start_guard.bat.example   # copy to start_guard.bat for auto-start
  └─ tools\                    # development and verification; need not be deployed
      ├─ _common.py            #   reuses DEFAULTS, generates fake clips/events
      ├─ selftest.py           #   pipeline + every route, without a camera
      ├─ devserver.py          #   runs the real app against a fake camera
      ├─ vendor.py             #   fetch static/ assets (build step, one-off)
      └─ calibrate.py          #   find a motion_threshold that fits the room

C:\CamRecordings\  # --root, created on start
  ├─ clips\        # 10-minute mp4 archive
  ├─ live\         # HLS scratch files (rotated by delete_segments)
  └─ events\       # the logbook. Date-named JSON Lines
```

`guard.py` and `index.html` must sit in the **same folder** (`main()` checks and exits otherwise).

### Inside guard.py

| Region | Role |
|---|---|
| `DEFAULTS` | The single source of truth for settings. argparse walks this dict to build CLI flags |
| `load_dotenv()` / `env_default()` | Precedence: **CLI flag > environment (.env) > DEFAULTS** (5-10) |
| `STATE` | Shared across threads (recording, started_at, restarts, last_error, events_today, last_event) |
| `CLIP_RE` | Filename convention. **Reused in three places: parsing, validation, path blocking** |
| `STATIC_RE` | `/static/` whitelist. Only ordinary names ending js/css/woff2 pass |
| `find_font()` / `timestamp_filter()` | Clock overlay. Missing or unusable font ⇒ **drop the filter, keep recording** |
| `list_devices()` | For `--list`. ffmpeg prints devices to stderr, so it is passed through |
| `build_input_args()` | The camera half of the argv, split out. **Tools swap only this for synthetic input** |
| `build_command()` | Assembles ffmpeg argv. `input_args=` replaces the input while output options stay real |
| `_job_handle()` / `adopt_child()` | Ties ffmpeg to a Windows Job Object to prevent orphans (5-9) |
| `supervisor()` | ffmpeg lifecycle (thread) |
| `janitor()` | Disk cleanup every 5 minutes (thread) |
| `analyzer()` | The logbook. Checks new segments every 2s and merges events (thread, 5-12..15) |
| `motion_score()` | **Percentage** of changed pixels. Do not swap in mean brightness (5-13) |
| `load_face_detector()` / `count_faces()` | Optional OpenCV. Returns `-1` when unavailable (5-14) |
| `make_handler()` | Builds the HTTP handler class, capturing config in a closure |
| `main()` | Prepare folders → start 3 threads (supervisor, janitor, analyzer) → serve |

---

## 5. Do not touch (decisions and their reasons)

Even when asked to change these, **explain the reason and offer an alternative**. Changing them casually breaks things quietly.

### 5-1. tee targets must be relative paths

```python
subprocess.Popen(cmd, cwd=str(root))   # chdir into root first
"[f=segment:...]clips/%Y-%m-%d_%H-%M-%S.mp4|[...]live/live.m3u8"
```

**Why**: the tee muxer uses `:` as its own option separator. An absolute path like `C:\CamRecordings\...` has the drive colon parsed as a separator and breaks. Escaping (`C\:\...`) can work but is extremely fragile. **Moving `cwd` and using relative paths is the verified solution.**

### 5-2. The janitor never deletes the newest file

```python
for p, st in files[:-1]:   # [:-1] is deliberate
```

**Why**: the last (newest) file in the list is the one ffmpeg is writing at this instant. Deleting it causes a file-lock error on Windows or cuts the recording stream.

### 5-3. The janitor swallows every exception

**Why**: a cleanup failure must never stop recording. For a security camera the top priority is that it keeps recording. Log it and wait for the next cycle.

### 5-4. `onfail=ignore` on the HLS output only

**Why**: tee kills the whole process when one output fails. Live view may break, but **the archive must survive**, so `onfail=ignore` goes on the HLS side. It must **not** go on the segment output — a recording that fails silently removes the reason this program exists.

### 5-5. Audio is off (`-an`)

**Why**: recording conversation can create legal problems depending on jurisdiction, and it is unnecessary for watching a room. If asked to enable it, raise this first.

### 5-6. Filename convention `YYYY-MM-DD_HH-MM-SS.mp4`

**Why**: this format satisfies three things at once — ① no characters Windows forbids (`:`) ② lexical order equals chronological order (the janitor depends on this) ③ the viewer's 24-hour timeline computes position by parsing the filename alone, so no database or index is needed.
Changing `CLIP_RE` affects the janitor's sort, `/api/clips` parsing and `/clips/` validation **simultaneously**.

### 5-7. `/clips/` validation is `CLIP_RE.match(name)`

**Why**: a whitelist. Strip directory components with `Path(p).name`, then pass only names matching the regex. Safer than a blacklist that blocks `..`. Verified (traversal → 404). `/static/` uses `STATIC_RE` the same way.

### 5-8. The drawtext font colon needs **two** backslashes

```python
esc = str(font).replace("\\", "/").replace(":", "\\\\:")   # -> C\\:/WINDOWS/Fonts/consola.ttf
```

**Why**: this bit us for real. The original code used `C\:/...` (one backslash), which **reliably fails on ffmpeg 9.0** — and not by dropping the overlay: **ffmpeg dies immediately and nothing is recorded at all.**

The cause is two layers of parsing. The filtergraph parser splits on `:` first, then drawtext splits its own options on `:` again. One backslash is consumed by the first parser, so a bare `:` arrives at the second.

Six spellings were actually run on 2026-08-23:

| Spelling | Result |
|---|---|
| `C\:/Windows/...` | **fails** (the original) |
| `C\\:/Windows/...` | works ← **adopted** |
| `'C\:/Windows/...'` (single-quoted) | works |
| `consola.ttf` (relative) | works |
| `/Windows/Fonts/...` (no drive letter) | works but **must not be used** — `cwd` is `--root`, so `--root D:\...` looks for the font on D: |

For the same reason `timestamp_filter()` **drops the filter entirely** when no font is found and keeps recording. Losing all footage to a decorative overlay is the worst trade available to a security camera.

### 5-9. ffmpeg is tied to a Job Object — do not remove `adopt_child()`

**Why**: `proc.terminate()` in `supervisor()` covers **only the clean shutdown path**. Task Manager kills, `Stop-Process -Force` and power cuts never run cleanup code, so ffmpeg survives, keeps holding the camera, and **from then on restarting never records again.** It was the most painful failure mode in continuous operation.

Windows closes every handle of a dying process. Putting ffmpeg in a `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` job therefore makes the OS kill the child **however we die**. That is also why `_JOB` is kept alive as a global — the moment the handle closes is the cleanup moment.

`ctypes` is standard library, so this does not violate the zero-dependency rule. On failure it logs and recording continues. `tools/selftest.py` hard-kills a parent every run and checks the child dies.

### 5-10. Precedence: CLI flag > environment (.env) > DEFAULTS

**Why this order**: `.env` means "the usual value"; a command-line flag means "different just this once". Reversed, `--port 9000` would lose to `.env` and be silently ignored.

The implementation is subtle. `env_default()` produces argparse's **default**, and argparse only overwrites it when a flag is actually given, so there is no need to track whether one was. Rewriting this to re-read `os.environ` after parsing **inverts the order** — do not.

Two more:

- **A real environment variable beats `.env`.** `load_dotenv()` never touches a key that already exists. A one-off `$env:GUARD_PASSWORD` losing to a file on disk would be surprising.
- **Check `bool` before `int`.** In Python `bool` subclasses `int`, so the other order sends `GUARD_TIMESTAMP=false` through `int("false")` and breaks.

`python-dotenv` was avoided for the zero-dependency reason in section 1. The `[config]` block in `tools/selftest.py` verifies this whole ordering.

### 5-11. `/live/*` is served as a validated snapshot, never streamed

```python
return self.send_live(live_dir / name, ctype)   # not send_file
```

**Why**: ffmpeg **truncates and rewrites** `live.m3u8` every two seconds. Serving it like `send_file` does — `stat()`, open, stream — breaks three ways. All three were reproduced on real hardware.

| Symptom | Cause |
|---|---|
| `PermissionError: [Errno 13]` + traceback | Windows refuses the open while ffmpeg holds the file (sharing violation) |
| Browser `ERR_CONTENT_LENGTH_MISMATCH` | The size from `stat()` differs from the bytes actually read |
| **No error at all**, half a playlist | Read after the truncate, before the rewrite. The most dangerous of the three |

`send_live()` reads the **whole file into memory at once** (live files are small — playlist ~300 B, segment ~400 KB) and, for `.m3u8`, **validates the content**: starts with `#EXTM3U`, ends with a newline. On failure it sleeps 30 ms and retries up to six times, then answers `503 + Retry-After`. Making the player ask again is better than letting it conclude the stream is dead.

The third symptom is why **retrying alone is not enough and content validation is mandatory** — there is no exception to catch.

> ffmpeg's `hls_flags=+temp_file` would swap atomically, but it is **not used.** If a reader holds the file open, the rename fails on Windows and this time the HLS output breaks. Defending in the server keeps the risk on one side.

Measured: under 12 concurrent threads for 45 seconds, server exceptions went 1 → 0. With keep-alive (how a real player behaves), 78242 requests were all clean.

### 5-12. The logbook reads **HLS segments** — not the camera, not the clips

**Why segments**: there are only three candidates and two are blocked.

| Candidate | Why not |
|---|---|
| Reopen the camera | **Impossible.** One process may hold a webcam on Windows (section 3) |
| Analyse the 10-minute clips | 10 minutes of latency, plus decoding ten minutes at once |
| **`live/seg*.ts`** | Appears every 2s, cheap to decode. **Adopted** |

Reading a file has nothing to do with the "one ffmpeg process" rule. That rule is about **who owns the camera**, not how many times ffmpeg runs. The analysis ffmpeg only touches finished files on disk.

Like the janitor, it **never touches the newest segment** (`segs[:-1]`) — ffmpeg is still writing it.

### 5-13. Motion is **percentage of changed pixels**, not mean brightness difference

```python
changed = sum(1 for x, y in zip(a, b) if abs(x - y) > PIXEL_NOISE)
worst = max(worst, changed * 100.0 / len(a))
```

**Why**: the first version used mean absolute difference and measurement proved it wrong. **A full-screen moving test pattern scored only 4.23**, so with a threshold of 6.0 a person walking through recorded nothing. Averaging dilutes a small subject.

Percentage of changed pixels solves three things at once — ① a person occupying 3% of the frame registers as 3% ② each pixel must move by `PIXEL_NOISE` to count, so sensor grain is ignored ③ a slow overall brightness drift does not move it. The value being "percent of the picture" also makes it easy for a user to tune.

**Measured (C920)**: empty room **0.00%**, a person moving 8–90%. The floor is genuinely zero because scaling to 64×36 averages each output pixel over a 20×20 block, erasing grain.

> Note that **the burnt-in clock does not register as motion.** The bottom strip was cropped on the assumption that it would, but measurement showed the text blurs away at 64×36 and the score is 0.00 either way. The crop stayed in case resolution or font size grows. selftest keeps checking this.

### 5-14. Face detection is **optional** and independent of motion

**Why optional**: ffmpeg has no face detection. Its `dnn_detect` filter needs an OpenVINO/TensorFlow backend compiled in, which general-purpose builds (Gyan included) do not ship — confirmed. So OpenCV is used when available, and **when it is not, `count_faces()` returns `-1`, motion is still recorded, and the program keeps running.** `guard.py`'s zero-pip promise holds.

**Why independent of motion**: gating faces behind motion to save CPU **misses someone sitting still entirely.** Measured cost is 78 ms per 2-second segment (4% of a core), which is affordable. The event condition is `motion >= threshold OR faces > 0`.

Because `faces` can be `-1`, comparisons must be `faces > 0` / `faces <= 0`. Writing `if faces:` makes `-1` truthy, turning **every segment into a face event** when OpenCV is absent.

### 5-15. Events are **merged into intervals** before being stored

**Why**: one line per segment means a person present for a minute produces 30 lines and the logbook becomes unreadable. When `event_gap` (default 10s) passes quietly, the event is closed and written as one line — `10:39:12–10:39:48, face 1, 36s`.

Storage is date-named **JSON Lines** (`events/YYYY-MM-DD.jsonl`). Append-only, so a crash mid-write costs one line rather than the file; readable in any editor; no database. `read_events()` skips broken lines.

**The clip is resolved at read time, not write time** (`/api/events`). At the moment an event occurs that clip is still being recorded, and this way the answer stays correct even if `segment_seconds` changed in between.

---

## 6. HTTP API contract

Every route requires HTTP Basic auth. The viewer (`index.html`) depends on these shapes, so **change both sides together**.

| Method | Path | Response |
|---|---|---|
| GET | `/` | `index.html` |
| GET | `/api/status` | `{channel, recording, uptime, restarts, last_error, used_gb, max_gb, retain_days, server_time, logbook, faces_available, events_today, last_event}` |
| GET | `/api/days` | `{days: ["2026-08-23", ...]}` newest first |
| GET | `/api/clips?date=YYYY-MM-DD` | `{clips: [{name, date, time, seconds_of_day, size_mb}]}` |
| GET | `/api/events?date=YYYY-MM-DD` | `{events: [{start, end, seconds_of_day, duration, kind, faces, score, clip, offset}]}` |
| GET | `/api/event-days` | `{days: [...]}` dates that have a logbook |
| GET | `/live/live.m3u8`, `/live/segN.ts` | HLS (no-store) |
| GET | `/clips/<name>.mp4` | mp4, **Range supported** (206) |
| GET | `/static/<name>.{js,css,woff2}` | vendored assets (`private, max-age=604800`) |

`seconds_of_day` is what the viewer's 24-hour timeline uses for position (`seconds_of_day / 86400 * 100%`).

---

## 7. Viewer design system (index.html)

Work within these tokens to keep it coherent. Do not add arbitrary new colours.

```css
--ink:#0D1117   /* background: black with a blue cast */
--panel:#151B24 /* panels */
--line:#25303F  /* borders */
--bone:#E6E1D5  /* body text (warm off-white) */
--dim:#78849A   /* secondary text */
--sodium:#F2A73B /* the accent — sodium street-lamp amber */
```

- **Fonts**: IBM Plex Mono (times, labels, numbers; uppercase with tracking), IBM Plex Sans (body). Chosen for the industrial feel of instruments and CCTV.
- **There is exactly one accent colour**: the record light, the selected item, the timeline, the capacity gauge, and the face marker in the logbook. Everything else is neutral.
- Mobile first. `prefers-reduced-motion` respected, keyboard focus rings present.
- **Assets are vendored in `static/`** (generated by `tools/vendor.py`). If the files are missing, hls.js falls back to a CDN and fonts fall back to system fonts. iOS Safari plays HLS natively and needs no hls.js at all.

### The signature element: the 24-hour bar (`.strip`)

A whole day on one horizontal bar. **Do not casually remove it** — it is the element that expresses what this project is.

With the logbook it became **two lanes**.

| Lane | Class | Meaning | Weight |
|---|---|---|---|
| top | `.tick` | a recorded 10-minute clip | `--sodium` 32% |
| bottom | `.evt` | a motion event | `--sodium` 55% |
| bottom | `.evt.face` | an event that contained a face | `--sodium` 100% |

**Why weight instead of colour**: the rule above forbids new colours. Three things need distinguishing, and adding colours would break that rule, so the hierarchy is built from one hue at different weights. The distinction weight cannot carry clearly (motion vs face) is stated **in words in the list** ("face 2" / "motion"). Colour is for scanning; words are for confirming.

### The logbook list (`.ev`)

Three columns: time, kind, duration. Clicking seeks to that moment (`playClipAt` → `playClip(..., offset)`).

**There is a guard against stale renders.** `loadEvents()` appends marks to `.strip` after an async fetch; if another render cleared the strip in between, two sets stack up (reproduced as 28 marks against a 14-row list). It compares `renderSeq` and draws nothing if its turn has passed. **Do not remove that check.**

---

## 8. Known limits and traps

| Item | Detail |
|---|---|
| Live latency | **3–6 seconds**, inherent to HLS. Reducing it needs WebRTC and a much more complex design |
| Encryption | **None (plain HTTP)**. Which is why the README pushes Tailscale over port forwarding |
| Camera contention | Zoom, Teams and the like take the webcam and ffmpeg cannot open it. Symptom: the `restarts` counter climbing |
| ~~Orphaned ffmpeg~~ **fixed** | Force-killing used to leave ffmpeg holding the camera. It is now in a Windows Job Object (`KILL_ON_JOB_CLOSE`) so the OS cleans up however guard.py dies. See 5-9; selftest verifies it with a real hard kill every run |
| Password exposure | `--password` stays in the command line where other processes can read it. `.env` / `GUARD_PASSWORD` avoids that |
| Assets not deployed | Without `static/`, the viewer falls back to a CDN. With no internet, Android live playback fails |
| Logbook CPU | 50 ms motion + 78 ms faces per 2-second segment = **about 6% of one core** (measured). `--no-faces` more than halves it |
| Face dependency | Needs OpenCV. Without it, motion is recorded silently — **an absence of face events is not necessarily a fault.** Check `faces_available` in `/api/status` |
| Logbook false positive | The **first segment after start** is logged, because auto-exposure settling changes the whole picture. Harmless, but it accumulates once per restart |
| Sleep | Recording stops when the machine sleeps. Disable sleep in power options |
| Windows service | **Do not.** Session 0 cannot reach the webcam. A startup shortcut (.bat) is correct |
| Disk | **15.4 GB/day** at 720p/15fps/1500k (measured). Set `--max-gb` below your free space or the system drive fills and Windows stops |
| Concurrent viewers | ThreadingHTTPServer. The live playlist was hammered with 12 threads (5-11), but not several people actually playing video |

---

## 9. Reasonable next work (suggested order)

1. **Watch long-run stability** — unverified item 1 in section 2. Leave it running for over a day and actually see the janitor delete and the watchdog restart. **This comes before anything else** — it has only been run for minutes at a time.
2. **Check the logbook in real use** — after a day has accumulated, open `events/` and see whether it is actually useful. Empty room but many events ⇒ raise `motion_threshold`; someone walked through but nothing logged ⇒ lower it. `tools/calibrate.py` supports that judgement. **Do not treat the 1.0% default as correct for every room.**
3. **A retention policy for the logbook** — nothing deletes `events/*.jsonl` today. At a few KB a day that is fine for years, but clips vanish after 10 days while entries pointing at them remain. The viewer already handles "no clip", so the question is whether to prune or keep.
4. **HTTPS** — a self-signed certificate, or Tailscale Serve for free TLS termination.
5. **Download / share button for a clip** — in the viewer.
6. **Multiple cameras** — settings become a list, folders split per channel. A large structural change; only when actually needed.

### Deliberately not doing

- **Alerts (push / Telegram / email)** — notifications containing false positives get muted within days, and from then on neither the alerts nor the logbook are trusted. Writing things down means checking when you want to, and skipping a wrong entry costs nothing. **If asked, explain this trade-off first.**
- **Face recognition that identifies people** — today it only answers "is there a face". Storing who someone is changes the nature of the data this program holds (biometrics). That is a question of kind, not of difficulty.
- **Audio** — 5-5.

---

## 10. Guidance for the next session

- **Ask the user for the verbatim error output.** Do not try to fix a dshow problem by guessing — whether the cause is the device name, an unsupported resolution or a busy camera is only distinguishable from the message.
- **Always include the "why"** in answers, and ask the user when information is missing (an explicit preference).
- After changing code, **always** run these two. Do not hand-type curl any more; the tools do that.
  ```bash
  python -m py_compile guard.py
  python tools/selftest.py
  ```
  `selftest.py` runs the real ffmpeg command against `lavfi testsrc` input, checking both tee outputs, the filename convention and the HLS playlist, then covers auth, JSON schemas, Range, path traversal, config precedence and logbook scoring. **If you changed the ffmpeg command, running this is the whole of the verification.**
- If you touched the viewer, use `python tools/devserver.py` — it feeds a fake camera (`lavfi`) into the real pipeline so you can look at it in a browser. No webcam needed, and it works while another app holds the camera.
- **The tools swap only `build_input_args()`.** Not mocking the output options is the point — what actually breaks is always the output side (tee escaping, filenames, HLS flags), so that part must be real code.
- Keep the **zero-dependency rule** when adding features. A pip package raises install difficulty sharply. (`tools/` is development-only and outside the rule, but even there only the standard library is used. OpenCV is **used if present, skipped if not**, which does not break it — 5-14.)
- **If you changed detection logic, recalibrate against a real camera.** Run `tools/calibrate.py` twice, empty room and occupied. Synthetic video cannot tell you a room's noise floor — this project had two designs overturned exactly there.

### Failure patterns that recurred here

Written down so the same trap is not walked into again. All three are **things believed correct until actually run.**

| Pattern | What happened |
|---|---|
| **Judging from synthetic tests alone** | Motion threshold 6.0. Plausible against synthetic patterns; measurement showed it missed people entirely (5-13) |
| **Saving CPU at the cost of the requirement** | Gating faces behind motion — someone sitting still was missed completely (5-14) |
| **Fixing at too narrow a scope** | The UTF-8 reconfigure went into `main()`, but threads and `tools/` never pass through it and died anyway. Module import was the right place |

The common lesson: **a failure that raises no exception is the dangerous kind.** A half-written playlist (5-11) and a logbook that records nothing (5-13) were both silently wrong, and both were caught because a test was written first.

---
---

<a name="한국어"></a>

# PROJECT.md — webcam-guard (한국어)

> 이 문서는 **다음 작업자(AI 세션 포함)를 위한 인수인계 문서**다.
> `README.md`는 최종 사용자용 설치 안내서이고, 이 파일은 설계 의도·검증 상태·수정 시 주의사항을 담는다.
> 이 문서는 **지금의 결론**을, [CHANGELOG.md](CHANGELOG.md)는 **거기까지 온 과정**과 되돌린 시도를 담는다.
> **코드를 수정하기 전에 끝까지 읽을 것.** 특히 "손대면 안 되는 것".

---

## 1. 프로젝트 개요

집에 남아도는 USB 웹캠을 **집을 비운 동안 방을 감시하는 카메라**로 쓰기 위한 시스템.

| 항목 | 결정 |
|---|---|
| 실행 환경 | **상시 켜두는 Windows PC** |
| 구현 방식 | **직접 코딩** (Frigate·motionEye 같은 기성품을 의도적으로 쓰지 않음) |
| 필수 기능 | ① 24시간 상시 녹화 ② 휴대폰에서 실시간 보기 ③ 오래된 영상 자동 삭제 ④ 움직임·얼굴 로그북 |
| 제외한 기능 | 오디오 녹음, 알림/푸시, **신원을 식별하는** 얼굴 인식, 클라우드 업로드 |
| 의존성 | ffmpeg(외부 실행 파일) + Python 표준 라이브러리만. **pip 패키지 0개.** OpenCV는 있으면 얼굴 감지에 쓰고 없으면 건너뛴다 |

④는 나중에 추가됐다. 장시간 운영에서는 **대부분의 시간에 아무 일도 없다**는 게 이유다. 상시 녹화는 유지하되 볼 곳을 찾아주는 색인이 필요했다. 근거는 5-12~15절.

---

## 2. 현재 상태 — 무엇이 검증됐고 무엇이 안 됐나

**이 구분이 가장 중요하다.** "미검증" 항목을 동작한다고 전제하고 작업하지 말 것.

### ✅ 실제 Windows + 실제 웹캠으로 검증됨 (2026-08-23/24)

검증 환경: Windows 11 Pro 26200 · Python 3.12.8 · **ffmpeg 9.0**(Gyan build, winget) · Logitech C920.

- **dshow 입력** — 실제 카메라로 녹화 + HLS 동시 출력 성공. raw(yuyv422)·mjpeg 둘 다 720p 15fps 실측.
- **drawtext 타임스탬프** — 화면에 시각이 찍히는 것까지 확인. **단, 이스케이프를 고친 뒤에야 됐다** (5-8절).
- **브라우저 HLS 재생** — Chromium에서 실시간 탭, 아카이브 탭의 24시간 타임라인·클립 재생 모두 확인.
- **오프라인 동작** — 뷰어 로드 시 외부 요청 **0건** (`static/` 내장 자산).
- `tee` 먹서 segment + HLS 동시 출력 / HTTP 전 라우트 / Basic 인증 / Range 206 / 경로 탈출 404.
- **라이브 파일 경합** — 동시 12스레드 45초 부하로 재현하고 수정 (5-11절). 이후 서버 예외 0건, keep-alive 78242회 전부 정상.
- 실측 용량: 720p·15fps·1500k에서 **하루 15.4GB** (1080p·15fps·2500k는 26.3GB).
- **로그북** — 종단 실행에서 얼굴 사건이 기록되고 `/api/events`가 해당 클립과 재생 위치까지 연결하는 것 확인.

**전부 `python tools/selftest.py` 한 줄로 재현된다. 54개 항목 통과.** 손으로 curl 치지 말 것.

### ❌ 아직 검증 안 됨

1. **장시간 안정성** — 24시간 이상 연속 구동, janitor의 실제 삭제(용량·기한 초과), 워치독의 실제 재시작. 최장 연속 구동은 수 분 수준이다.
2. **iOS Safari 네이티브 HLS** — 코드 경로는 있으나 실기기 미확인 (Chromium만 봤다).
3. **동시 시청 부하** — 라이브 재생목록은 12스레드로 두들겨봤지만(5-11절), 여러 명이 **실제로 영상을 재생하는** 대역폭은 안 봤다.

> **다음 세션의 첫 할 일**: 위 1번. 하루 이상 켜두고 `/api/status`의 `restarts`와 `used_gb`를 보는 것 외에 지름길이 없다.

---

## 3. 아키텍처

구조도는 위 영어 섹션의 그림과 같다.

### 왜 이 구조인가 — 핵심 제약

**Windows에서 웹캠은 한 번에 한 프로세스만 점유할 수 있다.**
그래서 "녹화용 ffmpeg 하나 + 스트리밍용 하나"는 **불가능**하다. ffmpeg 프로세스 **하나**가 카메라를 잡고 `tee` 먹서로 출력을 나눠야 한다. 인코딩도 한 번만 일어나 CPU도 절약된다.

이 제약에서 따라오는 것:

- **카메라가 필요한** 새 기능은 기존 ffmpeg 파이프라인 안에 넣어야 한다 (`select` 필터, 또는 tee의 세 번째 출력).
- 반면 **이미 기록된 파일을 읽는 건 자유롭다.** 로그북이 정확히 그 방식이다 (5-12절).
- 카메라를 다시 열려는 코드를 추가하면 즉시 깨진다.

---

## 4. 파일 구성

파일 트리는 위 영어 섹션과 같다. `guard.py`와 `index.html`은 **같은 폴더**에 있어야 한다(`main()`에서 검사 후 없으면 종료).

### guard.py 내부 구성

| 구역 | 역할 |
|---|---|
| `DEFAULTS` | 모든 설정의 단일 출처. argparse가 이 dict를 순회해 CLI 플래그를 자동 생성 |
| `load_dotenv()` / `env_default()` | 우선순위 **CLI 인자 > 환경변수(.env) > DEFAULTS** (5-10절) |
| `STATE` | 스레드 간 공유 상태 (recording, started_at, restarts, last_error, events_today, last_event) |
| `CLIP_RE` | 파일명 규약. **파싱·검증·경로차단 3곳에서 재사용** |
| `STATIC_RE` | `/static/` 화이트리스트. js/css/woff2 확장자의 평범한 이름만 통과 |
| `find_font()` / `timestamp_filter()` | 시각 오버레이. 폰트가 없거나 못 쓰면 **필터를 빼고 녹화는 계속** |
| `list_devices()` | `--list`용. ffmpeg이 장치 목록을 stderr로 뱉으므로 그대로 출력 |
| `build_input_args()` | 카메라 쪽(dshow) argv만 분리. **도구가 여기만 합성 입력으로 갈아끼운다** |
| `build_command()` | ffmpeg argv 조립. `input_args=`를 주면 입력만 대체되고 출력 옵션은 그대로 |
| `_job_handle()` / `adopt_child()` | ffmpeg을 Windows Job Object에 묶어 고아를 막는다 (5-9절) |
| `supervisor()` | ffmpeg 생명주기 관리 (스레드) |
| `janitor()` | 디스크 정리, 5분 주기 (스레드) |
| `analyzer()` | 로그북. 2초마다 새 세그먼트를 보고 사건을 병합 (스레드, 5-12~15절) |
| `motion_score()` | 변한 픽셀의 **비율**. 평균 밝기차로 바꾸지 말 것 (5-13절) |
| `load_face_detector()` / `count_faces()` | OpenCV 선택 사용. 없으면 `-1` (5-14절) |
| `make_handler()` | 클로저로 설정을 캡처해 HTTP 핸들러 클래스 생성 |
| `main()` | 폴더 준비 → 스레드 3개 기동(supervisor·janitor·analyzer) → HTTP 서버 blocking |

---

## 5. 손대면 안 되는 것 (설계 결정과 이유)

수정 요청이 들어와도 **이유를 설명하고 대안을 제시**할 것. 무심코 고치면 조용히 망가진다.

### 5-1. tee 타깃은 반드시 상대 경로

**Why**: tee 먹서는 `:`를 자체 옵션 구분자로 쓴다. 절대 경로 `C:\CamRecordings\...`는 드라이브 콜론이 구분자로 파싱돼 깨진다. 이스케이프로 해결할 수는 있으나 극도로 취약하다. **cwd를 옮기고 상대 경로를 쓰는 방식이 검증된 해법**이다.

### 5-2. janitor는 가장 최신 파일을 절대 삭제하지 않는다

`files[:-1]`은 의도적이다. 목록의 마지막(최신) 파일은 ffmpeg이 지금 쓰고 있다. 삭제하면 파일 잠금 오류가 나거나 녹화 스트림이 끊긴다.

### 5-3. janitor의 예외는 전부 삼킨다

청소 실패가 녹화를 멈춰선 안 된다. 감시 카메라의 최우선 순위는 "계속 녹화되는 것"이다.

### 5-4. HLS 출력에만 `onfail=ignore`

tee는 출력 하나가 실패하면 전체를 죽인다. 실시간이 깨져도 **아카이브는 살아야** 하므로 HLS 쪽에만 붙인다. segment 출력에 붙이면 **안 된다** — 녹화가 조용히 실패하면 이 프로그램의 존재 이유가 사라진다.

### 5-5. 오디오는 `-an`으로 꺼져 있다

대화 녹음은 지역에 따라 법적 문제가 되고, 방 감시라는 목적에도 불필요하다. 켜달라는 요청이 오면 이 점을 먼저 알릴 것.

### 5-6. 파일명 규약 `YYYY-MM-DD_HH-MM-SS.mp4`

세 가지를 동시에 만족한다 — ① Windows 금지 문자(`:`) 없음 ② 사전순 = 시간순 (janitor가 의존) ③ 뷰어가 파일명만 파싱해 타임라인 위치를 계산(DB·인덱스 불필요). `CLIP_RE`를 바꾸면 janitor 정렬·`/api/clips` 파싱·`/clips/` 검증이 **동시에** 영향받는다.

### 5-7. `/clips/` 경로 검증은 `CLIP_RE.match(name)`

화이트리스트 방식이다. `Path(p).name`으로 디렉터리 성분을 제거한 뒤 정규식에 맞는 이름만 통과시킨다. 블랙리스트보다 안전하다(traversal → 404 검증 완료). `/static/`도 `STATIC_RE`로 같은 방식이다.

### 5-8. drawtext 폰트 경로의 콜론은 백슬래시 **두 개**다

**Why**: 실제로 물린 버그다. 원래 `C\:/...`(1개)였고 **ffmpeg 9.0에서 확실히 실패한다** — 오버레이만 빠지는 게 아니라 **ffmpeg 전체가 죽어 녹화가 아예 안 된다.**

파서가 두 겹이기 때문이다. 필터그래프 파서가 먼저 `:`로 끊고, 그다음 drawtext가 자기 옵션을 다시 `:`로 끊는다. 백슬래시 1개는 첫 파서가 먹어 두 번째에는 맨 `:`가 도착한다.

2026-08-23에 6가지 표기를 실제로 돌린 결과:

| 표기 | 결과 |
|---|---|
| `C\:/Windows/...` | **실패** (원래 코드) |
| `C\\:/Windows/...` | 성공 ← **채택** |
| `'C\:/Windows/...'` (작은따옴표) | 성공 |
| `consola.ttf` (상대경로) | 성공 |
| `/Windows/Fonts/...` (드라이브 생략) | 성공하지만 **쓰면 안 된다** — `cwd`가 `--root`라 `--root D:\...`면 D드라이브에서 찾는다 |

같은 이유로 `timestamp_filter()`는 폰트를 못 찾으면 필터를 **통째로 빼고** 녹화를 계속한다.

### 5-9. ffmpeg은 Job Object에 묶여 있다 — `adopt_child()`를 빼지 말 것

`proc.terminate()`는 **정상 종료 경로만** 덮는다. 작업 관리자 종료·`Stop-Process -Force`·정전에는 정리 코드가 실행되지 않아 ffmpeg이 살아남고 카메라를 점유해 **그 뒤로는 재시작해도 영영 녹화가 안 된다.**

Windows는 죽는 프로세스의 핸들을 전부 닫는다. `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 잡에 넣어두면 **우리가 어떻게 죽든** OS가 자식을 같이 죽인다. `_JOB`을 전역으로 살려두는 것도 그래서다.

`ctypes`는 표준 라이브러리라 의존성 0개 원칙에 어긋나지 않는다. 실패하면 로그만 남기고 녹화는 계속한다. selftest가 매번 실제 하드킬로 확인한다.

### 5-10. 설정 우선순위: CLI 인자 > 환경변수(.env) > DEFAULTS

`.env`는 "평소 값", 인자는 "이번만 다르게"다. 반대로 두면 `--port 9000`이 조용히 무시된다.

`env_default()`가 argparse의 **default**를 만들고, argparse는 인자가 주어졌을 때만 덮는다. 그래서 "인자가 명시됐는지"를 추적할 필요가 없다. 파싱 후 `os.environ`을 다시 읽어 덮어쓰는 방식으로 바꾸면 **순서가 뒤집히니** 하지 말 것.

- **실제 환경변수가 `.env`보다 세다.** `load_dotenv()`는 이미 있는 키를 건드리지 않는다.
- **`bool` 검사를 `int`보다 먼저.** 파이썬에서 `bool`은 `int`의 하위 클래스라 순서가 바뀌면 `GUARD_TIMESTAMP=false`가 `int("false")`로 가서 깨진다.

### 5-11. `/live/*`는 스트리밍하지 말고 스냅샷으로 검증해서 보낸다

ffmpeg이 `live.m3u8`을 2초마다 **잘라내고 다시 쓴다.** `send_file`처럼 `stat()` → 열기 → 스트리밍하면 세 가지가 터진다. 전부 실기기에서 재현했다.

| 증상 | 원인 |
|---|---|
| `PermissionError: [Errno 13]` + 트레이스백 | ffmpeg이 파일을 쥔 순간 Windows가 열기를 거부 (공유 위반) |
| 브라우저 `ERR_CONTENT_LENGTH_MISMATCH` | `stat()` 크기와 실제 읽은 크기가 다름 |
| **아무 오류 없이** 반쪽짜리 재생목록 | 잘라낸 뒤 다시 쓰기 전에 읽음. 셋 중 가장 위험 |

`send_live()`는 파일 전체를 **한 번에 메모리로 읽고**(재생목록 ~300B, 세그먼트 ~400KB), `.m3u8`이면 `#EXTM3U`로 시작하고 개행으로 끝나는지 **내용을 검증**한다. 실패 시 30ms 간격 6회 재시도, 그래도 안 되면 `503 + Retry-After`.

세 번째 증상 때문에 **재시도만으로는 부족하고 내용 검증이 필수**다. 잡을 예외가 없기 때문이다.

> `hls_flags=+temp_file`로 원자적 교체를 시키는 방법은 **쓰지 않았다.** 리더가 파일을 연 상태면 Windows에서 rename이 실패해 이번엔 HLS 출력이 깨진다.

측정: 동시 12스레드 45초에서 서버 예외 1건 → 0건. keep-alive 78242회 전부 정상.

### 5-12. 로그북은 **HLS 세그먼트**를 읽는다 — 카메라도 클립도 아니다

| 후보 | 왜 안 되나 |
|---|---|
| 카메라를 다시 연다 | **불가능.** Windows에서 웹캠은 한 프로세스만 점유 (3절) |
| 10분짜리 클립을 분석 | 지연 10분 + 10분치를 한 번에 디코드 |
| **`live/seg*.ts`** | 2초마다 생기고 디코드가 싸다. **채택** |

파일을 읽는 것은 "ffmpeg 하나" 규칙과 무관하다. 그 규칙은 **카메라를 누가 쥐느냐**의 문제다. janitor처럼 **최신 세그먼트는 건드리지 않는다**(`segs[:-1]`).

### 5-13. 모션 지표는 평균 밝기차가 아니라 **변한 픽셀의 비율**이다

**Why**: 처음엔 평균 절대차였는데 실측에서 틀렸다는 게 드러났다. **화면 전체가 움직이는 테스트 패턴조차 4.23**이라 임계값 6.0으로는 사람이 지나가도 기록되지 않았다. 평균은 작은 피사체를 희석시킨다.

변한 픽셀 비율은 세 가지를 동시에 해결한다 — ① 화면의 3%를 차지하는 사람이 그대로 3%로 잡힌다 ② 픽셀마다 `PIXEL_NOISE`만큼 움직여야 세므로 센서 노이즈를 무시한다 ③ 전체 밝기가 서서히 변해도 흔들리지 않는다. "화면의 몇 %"라 사용자가 튜닝하기도 쉽다.

**실측(C920)**: 빈 방 **0.00%**, 사람 움직임 8~90%. 바닥이 진짜 0인 이유는 64×36으로 줄이면 한 픽셀이 원본 20×20을 평균내 노이즈가 지워지기 때문이다.

> **화면의 시계는 모션으로 잡히지 않는다.** 그럴 거라 보고 하단을 크롭했는데, 실측하니 64×36에서는 글자가 뭉개져 크롭 없이도 0.00이었다. 해상도·폰트를 키웠을 때를 대비해 남겨뒀고 selftest가 계속 확인한다.

### 5-14. 얼굴 감지는 **선택 사항**이고 모션과 독립이다

**Why 선택 사항인가**: ffmpeg에 얼굴 감지가 없다. `dnn_detect`는 OpenVINO/TensorFlow 백엔드를 넣어 빌드해야 하는데 일반 배포판(Gyan 포함)에는 없다 — 확인했다. OpenCV가 있으면 쓰고, **없으면 `count_faces()`가 `-1`을 돌려주고 모션만 기록한 채 계속 돈다.**

**Why 모션과 독립인가**: CPU를 아끼려 모션 뒤에 게이팅하면 **가만히 앉아 있는 사람을 통째로 놓친다.** 실측 78ms/2초(코어의 4%)라 항상 돌려도 된다. 조건은 `모션 ≥ 임계값 OR 얼굴 > 0`.

`faces`가 `-1`일 수 있으므로 비교는 반드시 `faces > 0` / `faces <= 0`. `if faces:`로 바꾸면 OpenCV가 없을 때 `-1`이 참이 되어 **전부 얼굴 사건이 된다.**

### 5-15. 사건은 **구간으로 병합**해서 저장한다

세그먼트마다 한 줄이면 1분 머문 사람이 30줄이 되어 읽을 수 없다. `event_gap`(기본 10초) 동안 조용하면 닫고 한 줄로 적는다.

저장은 날짜별 **JSON Lines**. 덧붙이기만 하므로 쓰다 죽어도 한 줄만 잃고, 아무 편집기로나 열리고, DB가 필요 없다. `read_events()`는 깨진 줄을 건너뛴다.

**클립 연결은 저장이 아니라 읽는 시점에** 한다. 사건이 난 순간 그 클립은 아직 녹화 중이고, `segment_seconds`가 바뀌어도 답이 맞아야 하기 때문이다.

---

## 6. HTTP API 계약

라우트 표는 위 영어 섹션과 동일하다. 모든 라우트에 Basic 인증이 적용되고, 뷰어가 이 스키마에 직접 의존하므로 **바꿀 때는 양쪽을 함께** 수정할 것.

`seconds_of_day`는 뷰어의 24시간 타임라인이 위치를 계산하는 값이다 (`seconds_of_day / 86400 * 100%`).

---

## 7. 뷰어 디자인 시스템

색 토큰은 위 영어 섹션과 같다. **임의의 새 색을 추가하지 말 것.**

- **폰트**: IBM Plex Mono(시각·라벨·수치), IBM Plex Sans(본문). 계측기·CCTV의 산업적 느낌을 의도했다.
- **강조색은 한 종류뿐이다**: 녹화 표시등, 선택 항목, 타임라인, 용량 게이지, 로그북의 얼굴 표시.
- 모바일 우선. `prefers-reduced-motion` 존중, 키보드 포커스 링 있음.
- **자산은 `static/`에 내장**되어 있다. 없으면 hls.js는 CDN으로, 폰트는 시스템 폰트로 내려간다.

### 시그니처 요소 = 24시간 타임라인 (`.strip`)

하루치를 가로 막대 하나에 뿌린다. **함부로 없애지 말 것.** 로그북이 들어오며 **두 레인**이 됐다.

| 레인 | 클래스 | 의미 | 농도 |
|---|---|---|---|
| 위 | `.tick` | 녹화된 10분 클립 | `--sodium` 32% |
| 아래 | `.evt` | 움직임 사건 | `--sodium` 55% |
| 아래 | `.evt.face` | 얼굴이 있던 사건 | `--sodium` 100% |

**Why 농도로 구분하나**: 위 규칙이 새 색 추가를 금지한다. 셋을 구분해야 하는데 색을 늘리면 규칙이 무너지므로 같은 색조의 농도로 위계를 만들었다. 농도만으로 애매한 구분(움직임 vs 얼굴)은 **목록에서 글자로** 말한다. 색은 훑어볼 때, 글자는 확인할 때 쓴다.

### 로그북 목록 (`.ev`)

`시각 · 종류 · 지속시간`. 누르면 그 시각으로 이동해 재생된다.

**낡은 렌더를 버리는 장치가 있다.** `loadEvents()`는 비동기로 받아온 뒤 스트립에 마커를 덧붙이는데, 그 사이 다른 렌더가 스트립을 비우면 두 벌이 겹친다(마커 28개 vs 목록 14개로 재현). `renderSeq`를 비교해 자기 차례가 지났으면 그리지 않는다. **이 검사를 지우지 말 것.**

---

## 8. 알려진 제약과 함정

표는 위 영어 섹션과 동일하다. 요점만:

- 실시간 지연 **3~6초**(HLS 특성), 통신은 **평문 HTTP** — 그래서 Tailscale을 권한다
- 다른 앱이 웹캠을 쓰면 ffmpeg이 못 연다. 증상은 `restarts` 증가
- 고아 ffmpeg은 **해결됨** (Job Object, 5-9절)
- 로그북 CPU **코어의 약 6%**, 얼굴은 OpenCV가 있어야 한다 — **얼굴 사건이 없다고 고장이 아닐 수 있다**(`faces_available` 확인)
- 기동 직후 첫 세그먼트는 자동노출 때문에 사건으로 기록된다(무해)
- 디스크 **하루 15.4GB**. `--max-gb`를 여유 공간보다 작게 잡지 않으면 Windows가 멈춘다
- Windows 서비스 등록은 **하지 말 것**(세션 0에서 웹캠 접근 불가)

---

## 9. 다음에 할 만한 작업

1. **장시간 안정성 관찰** — 2절 미검증 1번. **다른 무엇보다 먼저.** 아직 수 분 단위로만 돌려봤다.
2. **로그북 실사용 확인** — 하루치가 쌓인 뒤 `events/`를 열어볼 것. 빈 방인데 사건이 잔뜩이면 임계값을 올리고, 사람이 지나갔는데 비었으면 내린다. **기본값 1.0%를 정답으로 취급하지 말 것.**
3. **로그북 보존 정책** — 지금 `events/*.jsonl`은 아무도 지우지 않는다. 하루 몇 KB라 문제없지만, 클립은 10일 뒤 사라지는데 그 시간대를 가리키는 기록만 남는다.
4. **HTTPS** — 자체 서명 인증서 또는 Tailscale Serve.
5. **클립 다운로드/공유 버튼**
6. **다중 카메라** — 구조 변경이 크므로 필요할 때만.

### 하지 않기로 한 것

- **이벤트 알림(푸시/텔레그램/이메일)** — 오탐이 섞인 알림은 며칠이면 무시하게 되고, 그때부터 알림도 로그북도 신뢰를 잃는다. **요청이 오면 이 트레이드오프를 먼저 설명할 것.**
- **신원을 식별하는 얼굴 인식** — 누구인지 저장하면 이 프로그램이 다루는 데이터의 성격이 달라진다(생체정보). 난이도가 아니라 성격의 문제다.
- **오디오** — 5-5절.

---

## 10. 다음 세션 작업 지침

- **사용자에게 오류 메시지 원문을 요청할 것.** 추측으로 dshow 문제를 고치지 말 것 — 원인이 장치명인지, 해상도 미지원인지, 점유 충돌인지는 메시지로만 구분된다.
- **답변에는 항상 "왜"를 포함**하고, 필요한 정보는 되물을 것.
- 코드를 수정했으면 **반드시** 아래 두 줄. 손으로 curl 치지 말 것.
  ```bash
  python -m py_compile guard.py
  python tools/selftest.py
  ```
- 뷰어를 건드렸으면 `python tools/devserver.py`로 눈으로 확인.
- **도구는 `build_input_args()`만 갈아끼운다.** 출력 옵션을 mock 하지 않는 게 핵심이다 — 실제로 깨지는 건 항상 출력 쪽이다.
- **의존성 0개 원칙**을 지킬 것. (`tools/`는 개발 전용이라 원칙 밖이지만 거기서도 표준 라이브러리만 썼다. OpenCV는 **있으면 쓰고 없으면 건너뛰는** 방식이라 원칙을 깨지 않는다 — 5-14절.)
- **감지 로직을 건드렸으면 실제 카메라로 재보정할 것.** 합성 영상으로는 방의 노이즈 바닥을 알 수 없다 — 이 프로젝트에서 실제로 두 번 설계가 뒤집힌 지점이다.

### 이 프로젝트에서 반복된 실패 패턴

셋 다 **"돌려보기 전까지는 맞다고 믿었던 것"**이다.

| 패턴 | 실제 사례 |
|---|---|
| **합성 테스트만으로 판단** | 모션 임계값 6.0. 실측하면 사람이 안 잡혔다 (5-13절) |
| **CPU를 아끼려다 요구사항을 놓침** | 얼굴을 모션 뒤에 게이팅 → 가만히 있는 사람을 놓쳤다 (5-14절) |
| **고친 위치가 너무 좁음** | UTF-8 재설정을 `main()`에 넣었는데 스레드·도구는 거치지 않아 그대로 죽었다 |

공통 교훈: **예외가 나지 않는 실패가 제일 위험하다.** 반쪽짜리 재생목록(5-11절)과 아무것도 기록 안 되는 로그북(5-13절)은 둘 다 조용히 틀렸고, 테스트를 먼저 쓴 덕분에 잡혔다.
