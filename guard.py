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
)

STATE = {
    "recording": False,
    "started_at": None,
    "restarts": 0,
    "last_error": "",
}

CLIP_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})\.mp4$")

# Whitelist for /static/. Same reasoning as CLIP_RE: allow a known shape rather
# than blacklisting "..", so a name that is not plainly a vendored asset is 404.
STATIC_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.(js|css|woff2)$")
STATIC_TYPES = {".js": "text/javascript", ".css": "text/css", ".woff2": "font/woff2"}


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
                return self.send_file(live_dir / name, ctype)

            if p.startswith("/clips/"):
                name = Path(p).name
                if not CLIP_RE.match(name):
                    return self.not_found()
                return self.send_file(clips_dir / name, "video/mp4", ranged=True)

            return self.not_found()

    return Handler


# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Windows webcam security recorder")
    ap.add_argument("--list", action="store_true", help="list camera devices and exit")
    for k, v in DEFAULTS.items():
        flag = "--" + k.replace("_", "-")
        if isinstance(v, bool):
            ap.add_argument(flag, action="store_true", default=v)
            ap.add_argument("--no-" + k.replace("_", "-"), dest=k, action="store_false")
        else:
            ap.add_argument(flag, default=v, type=type(v) if v is not None else str)
    cfg = ap.parse_args()

    if cfg.list:
        return list_devices(cfg.ffmpeg)
    if not cfg.device:
        sys.exit('Missing --device. Run "python guard.py --list" to see camera names.')

    # An env var keeps the password out of the command line, where any other
    # process on the machine can read it (Task Manager's command line column,
    # `wmic process get commandline`, shell history). --password still works.
    cfg.password = os.environ.get("GUARD_PASSWORD") or cfg.password
    if cfg.password == DEFAULTS["password"]:
        print("[guard] 경고: 비밀번호가 기본값이야. "
              "GUARD_PASSWORD 환경변수나 --password 로 반드시 바꿔.", flush=True)

    root = Path(cfg.root)
    (root / "clips").mkdir(parents=True, exist_ok=True)
    (root / "live").mkdir(parents=True, exist_ok=True)
    web_dir = Path(__file__).resolve().parent
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
