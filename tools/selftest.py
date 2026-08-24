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
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (REPO, import_guard, make_cfg, prepare_root, have_ffmpeg,
                     seed_clips, seed_events)

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
# 0. configuration precedence
# ---------------------------------------------------------------------------
def test_config(tmp: Path) -> None:
    """CLI flag > environment (.env included) > DEFAULTS.

    Worth testing because it fails silently: get it backwards and the viewer
    quietly runs on the default password while the user believes their .env
    took effect.
    """
    env_file = tmp / ".env"
    env_file.write_text(
        "# comment line\n"
        "\n"
        "GUARD_PASSWORD = 'from-dotenv'\n"
        "export GUARD_PORT=9191\n"
        'GUARD_CHANNEL="ROOM 99"\n'
        "GUARD_TIMESTAMP=false\n"
        "GUARD_RETAIN_DAYS=not-a-number\n"
        "malformed line without equals\n",
        encoding="utf-8")

    keys = ["GUARD_PASSWORD", "GUARD_PORT", "GUARD_CHANNEL", "GUARD_TIMESTAMP",
            "GUARD_RETAIN_DAYS", "GUARD_USER"]
    saved = {k: os.environ.pop(k, None) for k in keys}
    os.environ["GUARD_USER"] = "already-set"   # a real env var must not be overwritten
    try:
        n = guard.load_dotenv(env_file)
        check("load_dotenv skips comments, blanks and malformed lines", n == 5,
              f"loaded {n} keys, expected 5")
        check("quotes are stripped and 'export' is tolerated",
              os.environ.get("GUARD_PASSWORD") == "from-dotenv"
              and os.environ.get("GUARD_PORT") == "9191"
              and os.environ.get("GUARD_CHANNEL") == "ROOM 99",
              f"{os.environ.get('GUARD_PASSWORD')!r} {os.environ.get('GUARD_PORT')!r}")
        check("a real environment variable beats .env",
              os.environ.get("GUARD_USER") == "already-set")
        check("env value overrides the built-in default",
              guard.env_default("password", "changeme") == "from-dotenv")
        check("typed values are converted", guard.env_default("port", 8088) == 9191)
        check("booleans understand false/no/off",
              guard.env_default("timestamp", True) is False)
        check("a non-numeric value falls back instead of crashing",
              guard.env_default("retain_days", 10) == 10)
        check("keys with no env var keep the default",
              guard.env_default("bitrate", "1500k") == "1500k")
        check("load_dotenv on a missing file is a no-op",
              guard.load_dotenv(tmp / "nope.env") == 0)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------------------
# 0b. logbook: scoring and storage (no camera, no ffmpeg)
# ---------------------------------------------------------------------------
def test_logbook(tmp: Path) -> None:
    w, h = guard.MOTION_W, guard.MOTION_H
    n = w * h
    still = bytes([100]) * n
    grain = bytes(100 + (i % 3) for i in range(n))
    half = bytes(200 if i < n // 2 else 100 for i in range(n))
    # Derived from the setting, not hardcoded: the smallest subject that is
    # supposed to be logged, so this test still means something if the default
    # threshold changes.
    need = int(n * guard.DEFAULTS["motion_threshold"] / 100) + 1
    speck = bytes(200 if i < need else 100 for i in range(n))

    check("identical frames score 0", guard.motion_score([still, still]) == 0.0)
    check("sub-threshold grain is ignored",
          guard.motion_score([still, grain]) == 0.0,
          f"got {guard.motion_score([still, grain])}")
    check("half the frame changing scores ~50",
          49 <= guard.motion_score([still, half]) <= 51,
          f"got {guard.motion_score([still, half])}")
    check(f"the smallest intended subject ({need} px) clears the threshold",
          guard.motion_score([still, speck]) >= guard.DEFAULTS["motion_threshold"],
          f"got {guard.motion_score([still, speck])}")
    check("score is the worst pair, not the average",
          guard.motion_score([still, half, still]) > 40)
    check("a single frame cannot score", guard.motion_score([still]) == 0.0)

    # storage round-trip
    events = tmp / "events"
    events.mkdir(parents=True, exist_ok=True)
    now = time.time()
    guard.write_event(events, {"start": now, "last": now + 42,
                               "score": 12.34, "faces": 2})
    guard.write_event(events, {"start": now, "last": now, "score": 3.0, "faces": 0})
    day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    rows = guard.read_events(events, day)
    check("write_event appends one JSON line per event", len(rows) == 2,
          f"got {len(rows)}")
    check("faces > 0 is recorded as a face event",
          rows[0]["kind"] == "face" and rows[0]["faces"] == 2
          and rows[0]["duration"] == 42, f"got {rows[0]}")
    check("no faces is recorded as motion", rows[1]["kind"] == "motion")
    check("seconds_of_day matches the start time",
          rows[0]["seconds_of_day"] == datetime.fromtimestamp(now).hour * 3600
          + datetime.fromtimestamp(now).minute * 60
          + datetime.fromtimestamp(now).second)

    # a crash mid-append leaves half a line; it must not poison the whole day
    with (events / f"{day}.jsonl").open("a", encoding="utf-8") as fh:
        fh.write('{"start": "10:00:00", "en')
    check("a half-written line is skipped, not fatal",
          len(guard.read_events(events, day)) == 2)
    check("unknown date returns nothing", guard.read_events(events, "1999-01-01") == [])
    check("a non-date is rejected before touching disk",
          guard.read_events(events, "../../guard.py") == [])

    # OpenCV is optional by design
    det = guard.load_face_detector()
    if det is None:
        check("no OpenCV -> count_faces reports -1, no crash",
              guard.count_faces(make_cfg(), tmp / "nope.ts") == -1)
    else:
        check("OpenCV present -> detector loaded", len(det) == 3)


def test_logbook_pipeline(root: Path) -> None:
    """Score real encoded video: a still scene must stay quiet, a moving one not.

    The still scene carries the burnt-in clock, because a repainting timestamp
    is the one thing guaranteed to change in an empty room.
    """
    cfg = make_cfg(size="640x360", fps=10)
    scenes = {
        "still room with clock": "color=c=gray:s=640x360:r=10",
        "moving subject": "testsrc=s=640x360:r=10",
    }
    scored = {}
    for label, src in scenes.items():
        out = root / (label.split()[0] + ".ts")
        cmd = [cfg.ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", src, "-t", "3"]
        cmd += guard.timestamp_filter(cfg)
        cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-f", "mpegts", str(out)]
        subprocess.run(cmd, check=True, capture_output=True)
        frames = guard.sample_frames(cfg, out, guard.MOTION_W, guard.MOTION_H,
                                     guard.MOTION_FPS)
        scored[label] = guard.motion_score(frames) if len(frames) >= 2 else -1.0

    thr = cfg.motion_threshold
    check(f"still room stays under the threshold ({scored['still room with clock']:.2f}%)",
          0 <= scored["still room with clock"] < thr)
    check(f"the burnt-in clock alone is not motion",
          scored["still room with clock"] < thr,
          "a repainting timestamp must not fill the logbook in an empty room")
    check(f"moving subject clears the threshold ({scored['moving subject']:.2f}%)",
          scored["moving subject"] >= thr)


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
    seed_events(root / "events", days=1)
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

        # The live playlist is rewritten by ffmpeg every couple of seconds.
        # Reading it straight off disk used to hit Windows sharing violations
        # (PermissionError) and Content-Length mismatches. Recreate that by
        # rewriting the file - at CHANGING lengths - while requesting it.
        stop_writer = threading.Event()
        playlist = root / "live" / "live.m3u8"

        def rewriter():
            n = 0
            while not stop_writer.is_set():
                n += 1
                # Varying length is the point: a fixed-size file would hide
                # Content-Length mismatches.
                body = "#EXTM3U\n#EXT-X-VERSION:3\n" + "".join(
                    f"#EXTINF:2.0,\nseg{i}.ts\n" for i in range(n % 9 + 1))
                try:
                    playlist.write_text(body)
                except OSError:
                    pass
                # ~200 rewrites/sec against ffmpeg's ~0.5. Still 400x harsher
                # than reality, but not a pathological zero-gap spin.
                time.sleep(0.005)

        w = threading.Thread(target=rewriter, daemon=True)
        w.start()
        bad = []
        try:
            for _ in range(400):
                st, h, body = c.get("/live/live.m3u8")
                if st != 200:
                    bad.append(f"status {st}")
                elif int(h.get("Content-Length", -1)) != len(body):
                    bad.append("Content-Length mismatch")
                elif not body.startswith(b"#EXTM3U"):
                    bad.append("truncated playlist")
        finally:
            stop_writer.set()
            w.join(timeout=5)
        check("live playlist survives concurrent rewrites (400 reads)",
              not bad, f"{len(bad)} bad responses, first: {bad[:3]}")
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
        print("\n[config]    .env parsing and the precedence chain")
        test_config(prepare_root(tmp / "cfg"))
        print("\n[logbook]   motion scoring, event storage, face detector")
        test_logbook(prepare_root(tmp / "log"))
        if run_pipe:
            if have_ffmpeg():
                print("\n[pipeline]  lavfi testsrc -> tee -> segment + hls")
                test_pipeline(prepare_root(tmp / "pipe"), timestamp)
                print("\n[motion]    still scene vs moving scene, real encoding")
                test_logbook_pipeline(prepare_root(tmp / "motion"))
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
