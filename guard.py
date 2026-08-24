#!/usr/bin/env python3
"""
guard.py - Windows webcam security recorder
  - ffmpeg 1 process -> tee muxer -> (1) 10min mp4 segments  (2) HLS live stream
  - watchdog: restarts ffmpeg if it dies
  - janitor: deletes oldest clips over size/age limit
  - http server: phone-friendly viewer, live + archive, HTTP Basic auth

Usage:
  python guard.py --list                 # find your camera name first
  python guard.py --device "HD WebCam"   # run
"""

import argparse
import base64
import json
import mimetypes
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

# Windows consoles and redirected pipes default to the legacy code page
# (cp949/cp1252), where the Korean status lines raise UnicodeEncodeError and
# kill whatever thread printed them - recording included. errors="replace"
# means the worst case is mojibake, never a crash.
#
# At import time, not inside main(): the analyzer thread and everything under
# tools/ import this module and print through it without going through main().
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


# ----------------------------------------------------------------------------
# defaults (override with CLI flags)
# ----------------------------------------------------------------------------
DEFAULTS = dict(
    ffmpeg="ffmpeg",
    device=None,
    size="1280x720",
    fps=15,
    bitrate="1500k",
    segment_seconds=600,      # 10 minutes per file
    root=r"C:\CamRecordings",
    # 10 days is the intent; the size cap is the guardrail behind it.
    # Measured 15.4 GB/day at 720p/15fps/1500k, so 10 days needs ~154 GB.
    # Keep max_gb comfortably above that or the budget silently wins and you
    # get fewer days than retain_days promises.
    max_gb=200.0,             # total archive budget
    retain_days=10,
    port=8088,
    user="admin",
    password="changeme",
    input_codec=None,         # try "mjpeg" if fps is low or input fails
    timestamp=True,           # burn clock into the picture
    channel="ROOM 01",
    # -- logbook ------------------------------------------------------------
    logbook=True,             # write motion/face events to events/<date>.jsonl
    # Measured on a real C920: an empty room reads 0.00%, a person moving
    # reads 8-90%. The floor is genuinely zero because scaling to 64x36
    # averages away sensor grain, so 1% sits far above noise and still
    # catches a head-sized subject. tools/calibrate.py retunes per room.
    motion_threshold=1.0,     # percent of the picture that must change
    faces=True,               # detect faces on every segment (needs OpenCV)
    event_gap=10,             # seconds of quiet before an event is closed
)

STATE = {
    "recording": False,
    "started_at": None,
    "restarts": 0,
    "last_error": "",
    "events_today": 0,
    "last_event": "",
}

CLIP_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})\.mp4$")

# Whitelist for /static/. Same reasoning as CLIP_RE: allow a known shape rather
# than blacklisting "..", so a name that is not plainly a vendored asset is 404.
STATIC_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.(js|css|woff2)$")
STATIC_TYPES = {".js": "text/javascript", ".css": "text/css", ".woff2": "font/woff2"}

SEG_RE = re.compile(r"^seg(\d+)\.ts$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Motion runs on a thumbnail: 64x36 grey is enough to see that something moved
# and costs nothing to diff in pure Python. Faces need real detail, so that
# pass decodes a larger frame - measured at 78ms per 2s segment.
MOTION_W, MOTION_H, MOTION_FPS = 64, 36, 4
PIXEL_NOISE = 20          # grey levels one pixel must move to count as changed
FACE_W, FACE_H, FACE_FPS = 480, 270, 2


# ----------------------------------------------------------------------------
# configuration: .env -> environment -> CLI flags
# ----------------------------------------------------------------------------
def load_dotenv(path: Path) -> int:
    """Read KEY=VALUE lines from a .env file into os.environ.

    A ~20 line parser instead of python-dotenv, because the zero-pip rule is
    the whole reason this installs with one command. Real environment variables
    win: a one-off `$env:GUARD_PASSWORD=...` should beat the file on disk.

    utf-8-sig because Notepad writes a BOM, which would otherwise become part
    of the first key name and silently do nothing.
    """
    if not path.is_file():
        return 0
    loaded = 0
    for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded


def env_default(key: str, fallback):
    """GUARD_<KEY> becomes the argparse default, so an explicit flag still wins.

    Precedence ends up as: CLI flag > environment (incl. .env) > DEFAULTS.
    """
    raw = os.environ.get("GUARD_" + key.upper())
    if raw is None:
        return fallback
    if isinstance(fallback, bool):  # before int: bool is a subclass of int
        return raw.strip().lower() not in ("", "0", "false", "no", "off")
    if isinstance(fallback, (int, float)):
        try:
            return type(fallback)(raw)
        except ValueError:
            print(f"[guard] GUARD_{key.upper()} 값이 숫자가 아니야: {raw!r} — 무시할게",
                  flush=True)
            return fallback
    return raw


# ----------------------------------------------------------------------------
# camera discovery
# ----------------------------------------------------------------------------
def list_devices(ffmpeg: str) -> None:
    """ffmpeg prints dshow devices to stderr; there is no clean JSON mode."""
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    print(proc.stderr)
    print('\nCopy the quoted video device name into --device "..."')


# ----------------------------------------------------------------------------
# ffmpeg
# ----------------------------------------------------------------------------
def find_font():
    """A monospace face for the clock overlay, or None if the box has none."""
    fonts = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Fonts"
    for name in ("consola.ttf", "cour.ttf", "arial.ttf", "segoeui.ttf"):
        p = fonts / name
        if p.is_file():
            return p
    return None


def timestamp_filter(cfg) -> list:
    """Burn the clock into the picture.

    Returns [] rather than a broken filter when no font is available. A bad
    font path aborts ffmpeg outright, which would trade ALL footage for a
    cosmetic overlay - the wrong trade for a security camera.
    """
    if not cfg.timestamp:
        return []
    font = find_font()
    if font is None:
        print("[guard] no usable font found; timestamp overlay disabled", flush=True)
        return []
    # The drive colon has to survive two parsers: the filtergraph splits the
    # description on ':', then drawtext splits its own options on ':' again.
    # The first parser eats one backslash, so the escape must be doubled.
    # Verified against ffmpeg 9.0 on Windows; tools/selftest.py keeps it honest.
    esc = str(font).replace("\\", "/").replace(":", "\\\\:")
    return ["-vf",
            f"drawtext=fontfile={esc}:text='%{{localtime}}':"
            "x=12:y=h-th-12:fontsize=22:fontcolor=white:"
            "box=1:boxcolor=black@0.45:boxborderw=6"]


def build_input_args(cfg) -> list:
    """The camera half of the command. Split out so tools/selftest.py can swap
    in a synthetic `lavfi` source and verify the output half without a webcam."""
    args = ["-f", "dshow", "-rtbufsize", "256M"]
    if cfg.input_codec:
        args += ["-vcodec", cfg.input_codec]
    return args + [
        "-video_size", cfg.size,
        "-framerate", str(cfg.fps),
        "-i", f"video={cfg.device}",
        "-an",  # no audio: recording voices at home has legal/privacy issues
    ]


def build_command(cfg, input_args=None) -> list:
    """
    Paths inside the tee target are RELATIVE on purpose: the tee muxer uses ':'
    as its own option separator, so an absolute Windows path (C:\\...) would
    need painful escaping. We chdir into the recordings root instead.
    """
    cmd = [cfg.ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostdin"]
    cmd += build_input_args(cfg) if input_args is None else list(input_args)

    cmd += timestamp_filter(cfg)

    cmd += [
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-tune", "zerolatency",
        "-pix_fmt", "yuv420p",
        "-b:v", cfg.bitrate, "-maxrate", cfg.bitrate, "-bufsize", "3000k",
        "-g", str(cfg.fps * 2), "-sc_threshold", "0",
    ]

    archive = (
        f"[f=segment:segment_time={cfg.segment_seconds}:segment_format=mp4"
        ":strftime=1:reset_timestamps=1]clips/%Y-%m-%d_%H-%M-%S.mp4"
    )
    live = (
        "[onfail=ignore:f=hls:hls_time=2:hls_list_size=6"
        ":hls_flags=delete_segments+omit_endlist+independent_segments"
        ":hls_segment_filename=live/seg%d.ts]live/live.m3u8"
    )
    cmd += ["-f", "tee", "-map", "0:v", f"{archive}|{live}"]
    return cmd


# ----------------------------------------------------------------------------
# windows: tie ffmpeg's lifetime to ours
# ----------------------------------------------------------------------------
_JOB = None  # kept alive for the life of the process, on purpose (see below)


def _job_handle():
    """A Windows job object that kills its members when the handle closes.

    Why this exists: terminate() in supervisor() only covers the graceful exit.
    Kill guard.py the hard way - Task Manager, Stop-Process -Force, a power cut
    - and ffmpeg survives, keeps the camera open, and nothing can reopen the
    device afterwards. The watchdog then restarts forever against a device that
    is permanently busy. Windows closes every handle a dying process owns, so
    hanging ffmpeg off a KILL_ON_JOB_CLOSE job makes the OS do the cleanup no
    matter how we die.

    ctypes is in the standard library, so the zero-pip-dependency rule holds.
    """
    global _JOB
    if _JOB is not None or sys.platform != "win32":
        return _JOB

    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BASIC_LIMIT(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BASIC_LIMIT),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        job = k32.CreateJobObjectW(None, None)
        if not job:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW")
        info = EXTENDED_LIMIT()
        info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(
                job, 9, ctypes.byref(info), ctypes.sizeof(info)):  # 9 = ExtendedLimit
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject")
        _JOB = job
    except Exception as e:
        # Losing this is survivable: recording still works, orphans just have
        # to be cleaned up by hand. Never let it stop the camera.
        print("[guard] job object unavailable, ffmpeg may outlive a hard kill:",
              e, flush=True)
        _JOB = False
    return _JOB


def adopt_child(proc) -> bool:
    """Put a child process into the kill-on-close job. Best effort."""
    job = _job_handle()
    if not job:
        return False
    import ctypes
    from ctypes import wintypes
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        # PROCESS_SET_QUOTA | PROCESS_TERMINATE - the rights a job needs.
        h = k32.OpenProcess(0x0100 | 0x0001, False, proc.pid)
        if not h:
            return False
        try:
            return bool(k32.AssignProcessToJobObject(job, h))
        finally:
            k32.CloseHandle(h)
    except Exception as e:
        print("[guard] could not adopt ffmpeg into the job:", e, flush=True)
        return False


def supervisor(cfg, root: Path, stop: threading.Event) -> None:
    """Keep exactly one ffmpeg alive. Backs off if it fails repeatedly."""
    backoff = 2
    while not stop.is_set():
        cmd = build_command(cfg)
        print("[ffmpeg]", " ".join(cmd), flush=True)
        try:
            proc = subprocess.Popen(
                cmd, cwd=str(root),
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                # ffmpeg writes UTF-8; without this a Korean device name in an
                # error line turns to mojibake in the viewer's status panel.
                text=True, encoding="utf-8", errors="replace",
            )
        except FileNotFoundError:
            STATE["last_error"] = "ffmpeg not found. Check --ffmpeg / PATH."
            print("[fatal]", STATE["last_error"], flush=True)
            return

        # Do this before anything else can go wrong: from here on, ffmpeg dies
        # with us even if we are killed without running any cleanup code.
        adopt_child(proc)

        STATE["recording"] = True
        STATE["started_at"] = time.time()

        for line in proc.stderr:
            line = line.strip()
            if line:
                STATE["last_error"] = line[:300]
                print("[ffmpeg]", line, flush=True)
            if stop.is_set():
                break

        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()

        STATE["recording"] = False
        if stop.is_set():
            return

        STATE["restarts"] += 1
        ran = time.time() - (STATE["started_at"] or 0)
        backoff = 2 if ran > 60 else min(backoff * 2, 60)
        print(f"[watchdog] ffmpeg exited after {ran:.0f}s, retry in {backoff}s", flush=True)
        stop.wait(backoff)


# ----------------------------------------------------------------------------
# disk janitor
# ----------------------------------------------------------------------------
def clip_files(clips_dir: Path):
    out = []
    for p in clips_dir.glob("*.mp4"):
        try:
            out.append((p, p.stat()))
        except OSError:
            pass
    return sorted(out, key=lambda t: t[0].name)


def janitor(cfg, clips_dir: Path, stop: threading.Event) -> None:
    """
    Delete oldest first, by age then by budget. Runs every 5 minutes.
    The newest file is never touched: ffmpeg is still writing into it.
    """
    max_bytes = int(cfg.max_gb * 1024 ** 3)
    while not stop.is_set():
        try:
            files = clip_files(clips_dir)
            cutoff = time.time() - cfg.retain_days * 86400
            for p, st in files[:-1]:
                if st.st_mtime < cutoff:
                    p.unlink(missing_ok=True)

            files = clip_files(clips_dir)
            total = sum(st.st_size for _, st in files)
            i = 0
            while total > max_bytes and i < len(files) - 1:
                p, st = files[i]
                p.unlink(missing_ok=True)
                total -= st.st_size
                i += 1
            if i:
                print(f"[janitor] removed {i} old clips", flush=True)
        except Exception as e:  # janitor must never kill the process
            print("[janitor] error:", e, flush=True)
        stop.wait(300)


# ----------------------------------------------------------------------------
# logbook: motion + faces
#
# Why the HLS segments and not the camera or the clips:
#   - the camera is already held by the recorder and Windows allows exactly one
#     holder, so re-opening it is impossible (see PROJECT.md 3);
#   - the 10 minute clips would mean 10 minutes of latency and re-decoding ten
#     minutes of video at once;
#   - live/seg*.ts are 2 second files that appear continuously and cost almost
#     nothing to decode.
# Reading a finished file off disk is unrelated to the one-process rule, which
# is about who owns the webcam.
# ----------------------------------------------------------------------------
_CV = None   # None = not tried, False = unavailable, tuple = loaded


def load_face_detector():
    """OpenCV + Haar cascade, or None.

    Deliberately optional. guard.py must keep running with zero pip packages,
    so a missing OpenCV downgrades the logbook to motion-only instead of
    turning into a startup error.
    """
    global _CV
    if _CV is not None:
        return _CV or None
    try:
        import cv2
        import numpy
        cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        if cascade.empty():
            raise RuntimeError("cascade data missing")
        _CV = (cv2, numpy, cascade)
        print("[logbook] 얼굴 감지 켜짐 (OpenCV)", flush=True)
    except Exception as e:
        print(f"[logbook] 얼굴 감지 꺼짐 — 모션만 기록해 ({e})", flush=True)
        _CV = False
    return _CV or None


def sample_frames(cfg, path: Path, w: int, h: int, fps: int) -> list:
    """Decode one segment into raw grayscale frames of w*h bytes.

    The bottom strip is cropped away because the burnt-in clock repaints every
    second. Without the crop the timestamp alone reads as motion and every
    single segment would be logged - verified, it fires on an empty room.
    """
    cmd = [cfg.ffmpeg, "-v", "error", "-nostdin", "-i", str(path),
           "-vf", f"crop=iw:ih*0.92:0:0,fps={fps},scale={w}:{h},format=gray",
           "-f", "rawvideo", "-"]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=60).stdout
    except (subprocess.TimeoutExpired, OSError):
        return []
    n = w * h
    return [out[i:i + n] for i in range(0, len(out) - n + 1, n)]


def motion_score(frames) -> float:
    """Largest percentage of the picture that changed between two frames.

    Counting *changed pixels* instead of averaging the difference is what makes
    a small subject visible: someone crossing 3% of the frame barely moves the
    mean brightness, but shows up plainly as 3% of pixels changed. Requiring
    each pixel to move PIXEL_NOISE levels also ignores sensor grain, and
    working per-pixel ignores slow brightness drift.

    Measured with a real C920: an empty room sits near 0, a hand waving reads
    several percent. Max over frame pairs, not average, so a brief movement in
    one pair is not diluted by the still pairs around it.
    """
    worst = 0.0
    for a, b in zip(frames, frames[1:]):
        changed = sum(1 for x, y in zip(a, b) if abs(x - y) > PIXEL_NOISE)
        worst = max(worst, changed * 100.0 / len(a))
    return worst


def count_faces(cfg, path: Path) -> int:
    """Most faces visible in any sampled frame; -1 when OpenCV is unavailable."""
    cv = load_face_detector()
    if cv is None:
        return -1
    cv2, numpy, cascade = cv
    best = 0
    for raw in sample_frames(cfg, path, FACE_W, FACE_H, FACE_FPS):
        # .copy() because frombuffer is read-only and OpenCV wants to write
        img = numpy.frombuffer(raw, dtype=numpy.uint8).reshape(FACE_H, FACE_W).copy()
        found = cascade.detectMultiScale(img, scaleFactor=1.15, minNeighbors=5,
                                         minSize=(30, 30))
        best = max(best, len(found))
    return best


def write_event(events_dir: Path, ev: dict) -> None:
    """Append one finished event to that day's logbook.

    JSON Lines: append-only, survives a crash mid-write (you lose one line,
    not the file), readable in any editor, and needs no database. The file is
    named by date so the viewer can fetch exactly one day.
    """
    start = datetime.fromtimestamp(ev["start"])
    end = datetime.fromtimestamp(ev["last"])
    rec = {
        "start": start.strftime("%H:%M:%S"),
        "end": end.strftime("%H:%M:%S"),
        "seconds_of_day": start.hour * 3600 + start.minute * 60 + start.second,
        "duration": max(1, int(ev["last"] - ev["start"])),
        "kind": "face" if ev["faces"] > 0 else "motion",
        "faces": ev["faces"],
        "score": round(ev["score"], 1),
    }
    path = events_dir / (start.strftime("%Y-%m-%d") + ".jsonl")
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    STATE["events_today"] += 1
    STATE["last_event"] = f'{rec["start"]} {rec["kind"]}'


def analyzer(cfg, root: Path, stop: threading.Event) -> None:
    """Watch live segments, log motion and faces. Never disturbs recording."""
    live_dir, events_dir = root / "live", root / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    done, open_ev = set(), None

    while not stop.is_set():
        try:
            segs = []
            for p in live_dir.glob("seg*.ts"):
                if SEG_RE.match(p.name):
                    try:
                        segs.append((p.stat().st_mtime, p))
                    except OSError:
                        pass
            segs.sort()

            # Skip the newest: ffmpeg is still writing it. Same reason the
            # janitor never touches its last file.
            for mtime, p in segs[:-1]:
                key = (p.name, int(mtime))
                if key in done:
                    continue
                done.add(key)

                frames = sample_frames(cfg, p, MOTION_W, MOTION_H, MOTION_FPS)
                if len(frames) < 2:
                    continue
                score = motion_score(frames)
                # Faces are checked on every segment, not only when motion
                # fired: someone sitting still is exactly what you want in the
                # logbook. Measured cost is 78ms per 2s segment (~4% of one
                # core) on top of 50ms for motion - affordable for a recorder
                # that is already encoding video around the clock.
                faces = count_faces(cfg, p) if cfg.faces else 0
                if score < cfg.motion_threshold and faces <= 0:
                    continue      # faces is -1 when OpenCV is unavailable

                if open_ev and mtime - open_ev["last"] <= cfg.event_gap:
                    open_ev["last"] = mtime            # same disturbance
                    open_ev["score"] = max(open_ev["score"], score)
                    open_ev["faces"] = max(open_ev["faces"], faces)
                else:
                    if open_ev:
                        write_event(events_dir, open_ev)
                    open_ev = {"start": mtime, "last": mtime,
                               "score": score, "faces": faces}

            # Close an event once the room has been quiet long enough.
            if open_ev and time.time() - open_ev["last"] > cfg.event_gap:
                write_event(events_dir, open_ev)
                open_ev = None

            # Bounded: keys for segments that have rotated away are dropped.
            done &= {(p.name, int(mt)) for mt, p in segs}
        except Exception as e:      # analysis must never kill recording
            print("[logbook] error:", e, flush=True)
        stop.wait(2)

    if open_ev:                     # do not lose the last event on shutdown
        try:
            write_event(events_dir, open_ev)
        except Exception:
            pass


def read_events(events_dir: Path, date: str) -> list:
    """Parse one day's logbook, skipping any line a crash left half-written."""
    path = events_dir / f"{date}.jsonl"
    if not DATE_RE.match(date) or not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


# ----------------------------------------------------------------------------
# http server
# ----------------------------------------------------------------------------
def make_handler(cfg, root: Path, web_dir: Path):
    clips_dir = root / "clips"
    live_dir = root / "live"
    static_dir = web_dir / "static"
    expected = base64.b64encode(f"{cfg.user}:{cfg.password}".encode()).decode()

    class Handler(BaseHTTPRequestHandler):
        server_version = "guard/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass  # ffmpeg output is noisy enough

        # -- auth ------------------------------------------------------------
        def authed(self) -> bool:
            hdr = self.headers.get("Authorization", "")
            if hdr.startswith("Basic ") and hdr[6:].strip() == expected:
                return True
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="guard"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        # -- helpers ---------------------------------------------------------
        def send_bytes(self, data: bytes, ctype: str, cache: str = "no-store"):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", cache)
            self.end_headers()
            self.wfile.write(data)

        def send_json(self, obj):
            self.send_bytes(json.dumps(obj).encode(), "application/json")

        def not_found(self):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def send_file(self, path: Path, ctype=None, ranged=False, cache=None):
            if not path.is_file():
                return self.not_found()
            ctype = ctype or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            size = path.stat().st_size
            rng = self.headers.get("Range", "") if ranged else ""

            if rng.startswith("bytes="):
                # single range only; enough for browser video seeking
                s, _, e = rng[6:].partition("-")
                start = int(s) if s else 0
                end = int(e) if e else size - 1
                end = min(end, size - 1)
                if start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(206)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                with path.open("rb") as f:
                    f.seek(start)
                    remaining = end - start + 1
                    while remaining > 0:
                        chunk = f.read(min(262144, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                return

            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            if ranged:
                self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", cache or
                             ("private, max-age=600" if ranged else "no-store"))
            self.end_headers()
            with path.open("rb") as f:
                shutil.copyfileobj(f, self.wfile, 262144)

        @staticmethod
        def playlist_is_complete(data: bytes) -> bool:
            """Reject a playlist caught mid-rewrite.

            ffmpeg truncates live.m3u8 and writes it again every couple of
            seconds, so a reader can legitimately observe an empty or
            half-written file. Nothing raises - the bytes are simply wrong -
            so the only defence is to check them: a finished playlist starts
            with the magic line and ends with a complete one.
            """
            return data.startswith(b"#EXTM3U") and data.endswith(b"\n")

        def send_live(self, path: Path, ctype: str):
            """Serve a file ffmpeg is rewriting underneath us.

            The HLS playlist is rewritten every couple of seconds, and two
            things go wrong if we stream it straight off disk:

              - Windows refuses the open while ffmpeg holds the file
                (PermissionError / sharing violation), which crashed the
                request thread and dumped a traceback;
              - the file can change between stat() and read, so the body no
                longer matches the Content-Length we already sent and the
                player gives up with ERR_CONTENT_LENGTH_MISMATCH;
              - the playlist can be read while it is truncated but not yet
                rewritten, which raises nothing at all and just serves a
                half-finished list.

            Reading one snapshot into memory fixes both: live files are small
            (playlist ~300 B, segment ~400 KB) and the length we announce is
            the length we are holding. Both failures were reproduced against a
            real camera before this existed.
            """
            for _ in range(6):
                try:
                    data = path.read_bytes()
                except FileNotFoundError:
                    return self.not_found()      # rotated away by delete_segments
                except OSError:
                    time.sleep(0.03)             # mid-rewrite; it is brief
                    continue
                if path.name.endswith(".m3u8") and not self.playlist_is_complete(data):
                    time.sleep(0.03)             # caught between truncate and write
                    continue
                return self.send_bytes(data, ctype)
            # Still busy after ~180ms. 503 tells the player to come back
            # instead of treating the whole stream as dead.
            self.send_response(503)
            self.send_header("Retry-After", "1")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def handle_one_request(self):
            try:
                super().handle_one_request()
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                # Phones abort video requests constantly - seeking, switching
                # tabs, screen off. Expected, so it should not look like an
                # error; a traceback per abort would bury the real ones.
                self.close_connection = True

        # -- routes ----------------------------------------------------------
        def do_GET(self):
            if not self.authed():
                return
            u = urlparse(self.path)
            p = unquote(u.path)

            if p in ("/", "/index.html"):
                return self.send_file(web_dir / "index.html", "text/html; charset=utf-8")

            if p == "/api/status":
                used = sum(st.st_size for _, st in clip_files(clips_dir))
                return self.send_json({
                    "channel": cfg.channel,
                    "recording": STATE["recording"],
                    "uptime": int(time.time() - STATE["started_at"]) if STATE["started_at"] else 0,
                    "restarts": STATE["restarts"],
                    "last_error": STATE["last_error"],
                    "used_gb": round(used / 1024 ** 3, 2),
                    "max_gb": cfg.max_gb,
                    "retain_days": cfg.retain_days,
                    "server_time": datetime.now().isoformat(timespec="seconds"),
                    "logbook": bool(cfg.logbook),
                    "faces_available": load_face_detector() is not None
                                       if cfg.logbook and cfg.faces else False,
                    "events_today": STATE["events_today"],
                    "last_event": STATE["last_event"],
                })

            if p == "/api/days":
                days = sorted({m.group(1) + "-" + m.group(2) + "-" + m.group(3)
                               for f, _ in clip_files(clips_dir)
                               if (m := CLIP_RE.match(f.name))}, reverse=True)
                return self.send_json({"days": days})

            if p == "/api/clips":
                want = (parse_qs(u.query).get("date") or [""])[0]
                items = []
                for f, st in clip_files(clips_dir):
                    m = CLIP_RE.match(f.name)
                    if not m:
                        continue
                    date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
                    if want and date != want:
                        continue
                    h, mi, s = int(m.group(4)), int(m.group(5)), int(m.group(6))
                    items.append({
                        "name": f.name,
                        "date": date,
                        "time": f"{h:02d}:{mi:02d}:{s:02d}",
                        "seconds_of_day": h * 3600 + mi * 60 + s,
                        "size_mb": round(st.st_size / 1024 ** 2, 1),
                    })
                return self.send_json({"clips": items})

            if p == "/api/events":
                want = (parse_qs(u.query).get("date") or [""])[0]
                events = read_events(root / "events", want)
                # Resolve each event to the clip that contains it, so the
                # viewer can jump straight to that moment. Done at read time,
                # not when the event is written: the clip is still being
                # recorded then, and this way the answer stays right even if
                # segment_seconds changed between then and now.
                clips = []
                for f, _ in clip_files(clips_dir):
                    m = CLIP_RE.match(f.name)
                    if m and f"{m.group(1)}-{m.group(2)}-{m.group(3)}" == want:
                        clips.append((int(m.group(4)) * 3600 + int(m.group(5)) * 60
                                      + int(m.group(6)), f.name))
                clips.sort()
                for ev in events:
                    sod = ev.get("seconds_of_day", 0)
                    ev["clip"], ev["offset"] = None, 0
                    for start, name in clips:
                        if start <= sod < start + cfg.segment_seconds:
                            ev["clip"], ev["offset"] = name, sod - start
                            break
                return self.send_json({"events": events})

            if p == "/api/event-days":
                days = sorted((f.stem for f in (root / "events").glob("*.jsonl")
                               if DATE_RE.match(f.stem)), reverse=True)
                return self.send_json({"days": days})

            if p.startswith("/static/"):
                name = Path(p).name
                if not STATIC_RE.match(name):
                    return self.not_found()
                # Vendored, versioned by content: cache hard so a phone on a
                # slow link does not refetch 400 KB of hls.js on every visit.
                return self.send_file(static_dir / name,
                                      STATIC_TYPES[Path(name).suffix],
                                      cache="private, max-age=604800")

            if p.startswith("/live/"):
                name = Path(p).name
                ctype = ("application/vnd.apple.mpegurl" if name.endswith(".m3u8")
                         else "video/mp2t")
                return self.send_live(live_dir / name, ctype)

            if p.startswith("/clips/"):
                name = Path(p).name
                if not CLIP_RE.match(name):
                    return self.not_found()
                return self.send_file(clips_dir / name, "video/mp4", ranged=True)

            return self.not_found()

    return Handler


# ----------------------------------------------------------------------------
def main():
    here = Path(__file__).resolve().parent
    n_env = load_dotenv(here / ".env")

    ap = argparse.ArgumentParser(description="Windows webcam security recorder")
    ap.add_argument("--list", action="store_true", help="list camera devices and exit")
    for k, v in DEFAULTS.items():
        flag = "--" + k.replace("_", "-")
        d = env_default(k, v)
        if isinstance(v, bool):
            ap.add_argument(flag, action="store_true", default=d)
            ap.add_argument("--no-" + k.replace("_", "-"), dest=k,
                            action="store_false", default=d)
        else:
            ap.add_argument(flag, default=d, type=type(v) if v is not None else str)
    cfg = ap.parse_args()

    if cfg.list:
        return list_devices(cfg.ffmpeg)
    if not cfg.device:
        sys.exit('Missing --device. Run "python guard.py --list" to see camera names.')

    # The password arrived through env_default("password"), i.e. GUARD_PASSWORD
    # from .env or the environment. Keeping it off the command line matters:
    # argv is readable by any other process on the machine (Task Manager's
    # command line column, `wmic process get commandline`, shell history).
    if n_env:
        print(f"[guard] .env 에서 설정 {n_env}개를 읽었어", flush=True)
    if cfg.password == DEFAULTS["password"]:
        print("[guard] 경고: 비밀번호가 기본값이야. "
              "GUARD_PASSWORD 환경변수나 --password 로 반드시 바꿔.", flush=True)

    root = Path(cfg.root)
    (root / "clips").mkdir(parents=True, exist_ok=True)
    (root / "live").mkdir(parents=True, exist_ok=True)
    web_dir = here
    if not (web_dir / "index.html").is_file():
        sys.exit("index.html must sit next to guard.py")
    if not (web_dir / "static" / "hls.min.js").is_file():
        # A warning, not an exit: the viewer falls back to the CDN copy, which
        # is fine as long as the phone can reach the internet.
        print("[guard] 경고: static/hls.min.js 가 없어 CDN 폴백으로 동작해. "
              "인터넷 없이 쓸 거면 python tools/vendor.py 를 한 번 돌려.", flush=True)

    stop = threading.Event()
    threading.Thread(target=supervisor, args=(cfg, root, stop), daemon=True).start()
    threading.Thread(target=janitor, args=(cfg, root / "clips", stop), daemon=True).start()
    if cfg.logbook:
        threading.Thread(target=analyzer, args=(cfg, root, stop), daemon=True).start()
        print(f"[logbook] 기록 중 → {root / 'events'}", flush=True)

    httpd = ThreadingHTTPServer(("0.0.0.0", cfg.port), make_handler(cfg, root, web_dir))
    httpd.daemon_threads = True

    def shutdown(*_):
        print("\n[guard] stopping...", flush=True)
        stop.set()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    print(f"[guard] recording to {root}")
    print(f"[guard] viewer at http://localhost:{cfg.port}  (user: {cfg.user})")
    try:
        httpd.serve_forever()
    finally:
        stop.set()
        print("[guard] stopped.")


if __name__ == "__main__":
    main()
