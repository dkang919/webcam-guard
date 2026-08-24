#!/usr/bin/env python3
"""
devserver.py - run the whole app with a fake camera, for viewer/UI work.

  python tools/devserver.py               # http://localhost:8099  (admin / dev)
  python tools/devserver.py --days 3      # seed more archive history
  python tools/devserver.py --no-live     # archive only, never call ffmpeg

Why this exists: Windows hands the webcam to one process at a time, and design
work usually happens while the camera is busy, missing, or on another OS. This
swaps ONLY the input half of the ffmpeg command for `lavfi testsrc` and then
runs the real supervisor, the real janitor and the real HTTP handler on top -
so what you see in the browser is the production code path, not a mock.

Recordings land in _scratch/devroot (gitignored). Nothing here ships.
"""

import shutil
import sys
import threading
import time
import webbrowser
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (REPO, have_ffmpeg, import_guard, make_cfg, prepare_root,
                     sample_clip_bytes, seed_clips)

guard = import_guard()

DEV_ROOT = REPO / "_scratch" / "devroot"
PORT = 8099
PASSWORD = "dev"


def use_fake_camera(cfg):
    """Replace the dshow input with a synthetic source.

    Patching build_input_args (not build_command) keeps every output option -
    the tee targets, the timestamp filter, the encoder settings - exactly as
    the real thing, which is the half that actually breaks.
    """
    def fake(_cfg):
        return ["-f", "lavfi", "-re",
                "-i", f"testsrc=size={_cfg.size}:rate={_cfg.fps}", "-an"]
    guard.build_input_args = fake


def main():
    args = sys.argv[1:]
    days = 2
    if "--days" in args:
        days = int(args[args.index("--days") + 1])
    live = "--no-live" not in args and have_ffmpeg()
    fresh = "--keep" not in args

    if fresh and DEV_ROOT.exists():
        shutil.rmtree(DEV_ROOT, ignore_errors=True)
    root = prepare_root(DEV_ROOT)

    cfg = make_cfg(device="FAKE CAM", password=PASSWORD, port=PORT,
                   root=str(root), channel="DEV ROOM", size="640x360", fps=10,
                   bitrate="600k", segment_seconds=60, max_gb=1.0)

    payload = sample_clip_bytes(seconds=3)
    n = seed_clips(root / "clips", days=days, payload=payload)
    print(f"[dev] seeded {n} fake clips over {days} day(s) "
          f"({'playable' if payload else 'placeholder bytes - install ffmpeg for playable ones'})")

    stop = threading.Event()
    if live:
        use_fake_camera(cfg)
        threading.Thread(target=guard.supervisor, args=(cfg, root, stop),
                         daemon=True).start()
        print("[dev] fake camera (lavfi testsrc) feeding the real ffmpeg pipeline")
    else:
        print("[dev] live tab disabled" + ("" if have_ffmpeg() else " - ffmpeg not on PATH"))

    threading.Thread(target=guard.janitor, args=(cfg, root / "clips", stop),
                     daemon=True).start()

    httpd = ThreadingHTTPServer(("127.0.0.1", PORT),
                                guard.make_handler(cfg, root, REPO))
    httpd.daemon_threads = True
    url = f"http://localhost:{PORT}"
    print(f"[dev] {url}   user: {cfg.user}  password: {PASSWORD}")
    print("[dev] Ctrl+C to stop")
    if "--no-open" not in args:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[dev] stopping...")
    finally:
        stop.set()
        time.sleep(0.3)


if __name__ == "__main__":
    main()
