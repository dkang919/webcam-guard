#!/usr/bin/env python3
"""
selftest.py - verify guard.py without a webcam. Run this after every change.

  python tools/selftest.py              # everything
  python tools/selftest.py --no-pipe    # skip the ffmpeg part (HTTP only)
  python tools/selftest.py --no-timestamp

Two halves:

  PIPELINE  Runs the REAL output half of build_command() against a synthetic
            `lavfi testsrc` input. This is the check PROJECT.md section 10 asks
            for: it proves the tee muxer really writes both targets, that the
            segment filenames match CLIP_RE, and - on Windows - that the
            drawtext font path survives escaping. Needs ffmpeg on PATH.

  HTTP      Starts the real handler over a temp recordings root and walks every
            route: auth, JSON schema, Range requests, path traversal.

Exit code 0 = all green. Anything else = read the FAIL lines.
"""

import base64
import http.client
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import REPO, import_guard, make_cfg, prepare_root, have_ffmpeg, seed_clips

guard = import_guard()

PASSED, FAILED = [], []


def check(name, ok, detail=""):
    (PASSED if ok else FAILED).append(name)
    mark = "  ok  " if ok else " FAIL "
    print(f"[{mark}] {name}" + (f"\n         {detail}" if detail and not ok else ""))
    return ok


# ---------------------------------------------------------------------------
# 1. ffmpeg pipeline, synthetic input
# ---------------------------------------------------------------------------
def test_pipeline(root: Path, timestamp: bool) -> None:
    seconds, seg = 9, 3
    cfg = make_cfg(device="SELFTEST", segment_seconds=seg, size="320x180", fps=10,
                   bitrate="400k", timestamp=timestamp)
    # -re matters: segment filenames come from strftime, i.e. the WALL CLOCK at
    # the moment each file is opened. Encoding 9s as fast as possible would open
    # every segment within the same second and they would overwrite each other.
    # A camera feeds in real time, so real time is what we have to simulate.
    lavfi = ["-f", "lavfi", "-re", "-i", f"testsrc=size={cfg.size}:rate={cfg.fps}",
             "-t", str(seconds), "-an"]
    cmd = guard.build_command(cfg, input_args=lavfi)

    proc = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True,
                          errors="replace", timeout=180)
    tail = "\n         ".join((proc.stderr or "").strip().splitlines()[-4:])
    if not check("ffmpeg pipeline exits cleanly", proc.returncode == 0, tail):
        if timestamp and "drawtext" in (proc.stderr or "").lower():
            print("         -> drawtext failed; run guard.py with --no-timestamp")
        return

    clips = sorted((root / "clips").glob("*.mp4"))
    check(f"segment target wrote mp4 files ({len(clips)})", len(clips) >= 2)
    bad = [c.name for c in clips if not guard.CLIP_RE.match(c.name)]
    check("segment filenames match CLIP_RE", not bad, f"unmatched: {bad}")
    check("clips are non-empty", all(c.stat().st_size > 0 for c in clips))

    m3u8 = root / "live" / "live.m3u8"
    segs = list((root / "live").glob("seg*.ts"))
    check("hls target wrote live.m3u8", m3u8.is_file())
    check(f"hls target wrote .ts segments ({len(segs)})", len(segs) >= 1)
    if m3u8.is_file():
        check("playlist has no ENDLIST (live, not VOD)",
              "#EXT-X-ENDLIST" not in m3u8.read_text(errors="replace"))
    if timestamp:
        check("drawtext timestamp overlay accepted", True)


# ---------------------------------------------------------------------------
# 1b. orphan protection (Windows)
# ---------------------------------------------------------------------------
def pid_alive(pid: int) -> bool:
    """tasklist, because os.kill(pid, 0) TERMINATES on Windows rather than probe."""
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                         capture_output=True, text=True, errors="replace").stdout
    return str(pid) in out


def test_orphan_kill() -> None:
    """Hard-kill a parent and prove its ffmpeg child dies with it.

    This is the exact failure mode the job object exists for, so it is tested
    the exact way it happens: Popen.kill() is TerminateProcess on Windows, the
    same thing Task Manager does. No cleanup handler gets a chance to run.
    """
    helper = (
        "import subprocess,sys,time;"
        f"sys.path.insert(0,r'{REPO}');"
        "import guard;"
        # -re: without it ffmpeg burns through 120s of synthetic video in an
        # instant and exits before we can test anything.
        "p=subprocess.Popen(['ffmpeg','-hide_banner','-loglevel','quiet',"
        "'-re','-f','lavfi','-i','testsrc=size=64x36:rate=5','-t','120',"
        "'-f','null','-'],"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
        "print(guard.adopt_child(p),p.pid,flush=True);"
        "time.sleep(120)"
    )
    parent = subprocess.Popen([sys.executable, "-c", helper],
                              stdout=subprocess.PIPE, text=True)
    try:
        line = parent.stdout.readline().split()
        adopted, child_pid = line[0] == "True", int(line[1])
        if not check("ffmpeg adopted into the kill-on-close job", adopted):
            return
        if not check("adopted child is running", pid_alive(child_pid)):
            return
        parent.kill()               # TerminateProcess: no cleanup path runs
        parent.wait(timeout=15)
        deadline = time.time() + 10
        while pid_alive(child_pid) and time.time() < deadline:
            time.sleep(0.3)
        check("hard-killing guard.py takes ffmpeg with it (no orphan)",
              not pid_alive(child_pid),
              f"pid {child_pid} still alive - it would keep holding the camera")
    finally:
        if parent.poll() is None:
            parent.kill()


# ---------------------------------------------------------------------------
# 2. http routes
# ---------------------------------------------------------------------------
class Client:
    """Raw http.client so paths are sent verbatim - urllib would normalise
    '..' away client-side and the traversal test would prove nothing."""

    def __init__(self, port, user, password):
        self.port = port
        self.auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    def get(self, path, headers=None, auth=True):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = dict(headers or {})
        if auth:
            h["Authorization"] = self.auth
        c.putrequest("GET", path, skip_accept_encoding=True)
        for k, v in h.items():
            c.putheader(k, v)
        c.endheaders()
        r = c.getresponse()
        body = r.read()
        status, hdrs = r.status, dict(r.getheaders())
        c.close()
        return status, hdrs, body


def test_http(root: Path) -> None:
    cfg = make_cfg(device="SELFTEST", password="selftest-pw", port=0)
    n = seed_clips(root / "clips", days=1)
    (root / "live" / "live.m3u8").write_text("#EXTM3U\n#EXT-X-VERSION:3\n")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), guard.make_handler(cfg, root, REPO))
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    c = Client(port, cfg.user, cfg.password)
    try:
        st, _, _ = c.get("/api/status", auth=False)
        check("no credentials -> 401", st == 401, f"got {st}")

        st, _, _ = Client(port, cfg.user, "wrong").get("/api/status")
        check("wrong password -> 401", st == 401, f"got {st}")

        st, _, body = c.get("/")
        check("GET / serves the viewer", st == 200 and b"<title>Guard" in body, f"got {st}")

        st, _, body = c.get("/api/status")
        s = json.loads(body or b"{}")
        want = {"channel", "recording", "uptime", "restarts", "last_error",
                "used_gb", "max_gb", "retain_days", "server_time"}
        missing = want - set(s)
        check("/api/status schema matches the viewer's contract",
              st == 200 and not missing, f"missing keys: {missing}")

        st, _, body = c.get("/api/days")
        days = json.loads(body or b"{}").get("days", [])
        check("/api/days lists days newest first",
              st == 200 and len(days) >= 1 and days == sorted(days, reverse=True))

        st, _, body = c.get(f"/api/clips?date={days[0]}")
        clips = json.loads(body or b"{}").get("clips", [])
        ok_shape = bool(clips) and all(
            0 <= x["seconds_of_day"] < 86400 and x["date"] == days[0] for x in clips)
        check(f"/api/clips returns that day only ({len(clips)} of {n})",
              st == 200 and ok_shape)

        name = clips[0]["name"]
        st, h, body = c.get(f"/clips/{name}", {"Range": "bytes=0-99"})
        check("mp4 Range request -> 206 with exact byte count",
              st == 206 and len(body) == 100
              and h.get("Content-Range", "").startswith("bytes 0-99/"),
              f"status={st} len={len(body)} range={h.get('Content-Range')}")

        st, h, body = c.get(f"/clips/{name}")
        check("mp4 full request -> 200 + Accept-Ranges",
              st == 200 and h.get("Accept-Ranges") == "bytes")

        st, _, _ = c.get("/clips/../guard.py")
        check("path traversal /clips/../guard.py -> 404", st == 404, f"got {st}")

        st, _, _ = c.get("/clips/not-a-clip.mp4")
        check("non-conforming clip name -> 404", st == 404, f"got {st}")

        st, h, _ = c.get("/live/live.m3u8")
        check("HLS playlist served as no-store",
              st == 200 and h.get("Cache-Control") == "no-store", f"status={st}")

        # /static/ - vendored assets. Missing files are a warning, not a
        # failure: guard.py works without them via the viewer's CDN fallback.
        if (REPO / "static" / "hls.min.js").is_file():
            st, h, body = c.get("/static/hls.min.js")
            check("vendored hls.js served with a long cache",
                  st == 200 and len(body) > 200_000
                  and h.get("Content-Type") == "text/javascript"
                  and "max-age=604800" in h.get("Cache-Control", ""),
                  f"status={st} len={len(body)} ct={h.get('Content-Type')}")

            st, h, body = c.get("/static/fonts.css")
            check("vendored fonts.css served as text/css",
                  st == 200 and b"@font-face" in body
                  and h.get("Content-Type") == "text/css", f"status={st}")

            st, _, _ = c.get("/static/../guard.py")
            check("path traversal /static/../guard.py -> 404", st == 404, f"got {st}")

            st, _, _ = c.get("/static/secrets.txt")
            check("non-asset extension under /static/ -> 404", st == 404, f"got {st}")
        else:
            print("[ skip ] /static/ checks - run tools/vendor.py first")

        st, _, _ = c.get("/nope")
        check("unknown route -> 404", st == 404, f"got {st}")
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---------------------------------------------------------------------------
def main():
    args = sys.argv[1:]
    run_pipe = "--no-pipe" not in args
    timestamp = "--no-timestamp" not in args

    print("guard selftest")
    print("=" * 62)

    tmp = Path(tempfile.mkdtemp(prefix="guard-selftest-"))
    try:
        if run_pipe:
            if have_ffmpeg():
                print("\n[pipeline]  lavfi testsrc -> tee -> segment + hls")
                test_pipeline(prepare_root(tmp / "pipe"), timestamp)
                if sys.platform == "win32":
                    print("\n[orphan]    hard-kill the parent, child must die too")
                    test_orphan_kill()
            else:
                print("\n[pipeline]  SKIPPED - ffmpeg not on PATH "
                      "(winget install Gyan.FFmpeg)")
        print("\n[http]      every route, over a temp recordings root")
        test_http(prepare_root(tmp / "http"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("=" * 62)
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    for f in FAILED:
        print("  x", f)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
