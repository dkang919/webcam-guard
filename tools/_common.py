"""
Shared helpers for the dev tools in this folder.

These tools exist so the project can be worked on WITHOUT a webcam: Windows
gives the camera to one process only, and most development happens on a machine
where the camera is busy, absent, or the OS is not Windows at all.

Nothing in here is imported by guard.py. Keep it that way - guard.py must stay
a single self-contained file the user can copy anywhere.
"""

import argparse
import os
import shutil
import subprocess
import tempfile
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def import_guard():
    """Import guard.py from the repo root regardless of the current directory."""
    sys.path.insert(0, str(REPO))
    import guard  # noqa: E402
    return guard


def make_cfg(**overrides):
    """A config object shaped exactly like argparse's, seeded from DEFAULTS.

    DEFAULTS is the single source of truth for settings, so the tools inherit
    new options automatically instead of drifting from guard.py.
    """
    guard = import_guard()
    cfg = argparse.Namespace(**guard.DEFAULTS)
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def prepare_root(root: Path) -> Path:
    (root / "clips").mkdir(parents=True, exist_ok=True)
    (root / "live").mkdir(parents=True, exist_ok=True)
    return root


def have_ffmpeg(ffmpeg="ffmpeg") -> bool:
    return shutil.which(ffmpeg) is not None


def sample_clip_bytes(ffmpeg="ffmpeg", seconds=4) -> bytes | None:
    """A tiny real mp4 so seeded clips actually play in the viewer.

    Returns None when ffmpeg is missing; callers fall back to placeholder bytes,
    which still exercise the listing, the timeline and the janitor.
    """
    if not have_ffmpeg(ffmpeg):
        return None
    # A real temp dir, NOT REPO/_scratch: the cleanup below is a recursive
    # delete, and _scratch is where devserver keeps its recordings root. This
    # used to wipe the folder it had just been asked to fill.
    tmp = Path(tempfile.mkdtemp(prefix="guard-sample-"))
    out = tmp / "_sample.mp4"
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc=size=320x180:rate=10",
         "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast",
         "-pix_fmt", "yuv420p", str(out)],
        capture_output=True,
    )
    data = out.read_bytes() if out.is_file() else None
    shutil.rmtree(tmp, ignore_errors=True)
    return data


def seed_events(events_dir: Path, days=2, per_day=14, end: datetime | None = None) -> int:
    """Fake logbook entries so the viewer's event lane can be worked on.

    Clustered around waking hours rather than spread evenly - an empty night
    and a busy evening is what the real strip looks like, and a uniform
    sprinkle would hide layout problems the real shape exposes.
    """
    import json
    import random

    events_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(7)          # fixed seed: same picture every run
    end = end or datetime.now()
    written = 0
    for d in range(days):
        day = (end - timedelta(days=d)).date()
        rows = []
        for _ in range(per_day):
            hour = rng.choice([7, 8, 9, 12, 13, 18, 19, 20, 21, 22])
            start = datetime.combine(day, datetime.min.time()).replace(
                hour=hour, minute=rng.randrange(60), second=rng.randrange(60))
            duration = rng.choice([4, 8, 15, 36, 90, 240])
            faces = rng.choice([0, 0, 0, 1, 1, 2])
            rows.append({
                "start": start.strftime("%H:%M:%S"),
                "end": (start + timedelta(seconds=duration)).strftime("%H:%M:%S"),
                "seconds_of_day": start.hour * 3600 + start.minute * 60 + start.second,
                "duration": duration,
                "kind": "face" if faces else "motion",
                "faces": faces,
                "score": round(rng.uniform(1.2, 95.0), 1),
            })
        rows.sort(key=lambda r: r["seconds_of_day"])
        path = events_dir / (day.strftime("%Y-%m-%d") + ".jsonl")
        with path.open("w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
                written += 1
    return written


def seed_clips(clips_dir: Path, days=2, every_seconds=600, payload=None,
               end: datetime | None = None) -> int:
    """Write files named exactly like ffmpeg's segment output.

    The name IS the index in this project (no database), so seeding has to obey
    CLIP_RE: YYYY-MM-DD_HH-MM-SS.mp4. mtime is set to the encoded time too,
    because the janitor deletes by mtime.
    """
    payload = payload if payload is not None else os.urandom(48_000)
    end = end or datetime.now().replace(second=0, microsecond=0)
    end -= timedelta(minutes=end.minute % (every_seconds // 60 or 1))
    n = 0
    for i in range(int(days * 86400 / every_seconds)):
        t = end - timedelta(seconds=i * every_seconds)
        f = clips_dir / t.strftime("%Y-%m-%d_%H-%M-%S.mp4")
        f.write_bytes(payload)
        ts = t.timestamp()
        os.utime(f, (ts, ts))
        n += 1
    return n
