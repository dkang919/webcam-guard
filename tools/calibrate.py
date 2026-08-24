#!/usr/bin/env python3
"""
calibrate.py - pick a motion_threshold that suits YOUR room.

  python tools/calibrate.py                 # 30s from the camera in .env
  python tools/calibrate.py --seconds 60
  python tools/calibrate.py --from-live     # reuse a running guard.py instead

Why this exists: the shipped default (1.0% of the picture changed) was measured
in one room with one camera. A darker room, a noisier sensor, a TV or a window
in frame all move the floor. Rather than guess, record a sample and look at the
numbers.

How to use it: run it twice.
  1. Leave the room empty  -> every line should read ~0. That is your floor.
  2. Walk through the frame -> those lines are what you want caught.
Set GUARD_MOTION_THRESHOLD between the two, nearer the floor.

Recording from the camera needs the camera free, so stop guard.py first, or
use --from-live to analyse the segments a running guard.py is already writing.
"""

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import import_guard, make_cfg

guard = import_guard()


def record_sample(cfg, out_dir: Path, seconds: int) -> list:
    """Capture the room in 2s chunks shaped exactly like guard.py's HLS output."""
    cmd = ([cfg.ffmpeg, "-v", "error", "-nostdin"]
           + guard.build_input_args(cfg) + ["-t", str(seconds)]
           + guard.timestamp_filter(cfg)
           + ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
              # Without -g the segment muxer can only cut on rare keyframes and
              # you get two long files instead of many 2s ones.
              "-g", str(cfg.fps * 2), "-sc_threshold", "0",
              "-f", "segment", "-segment_time", "2",
              "-segment_format", "mpegts", str(out_dir / "seg%03d.ts")])
    print(f"{cfg.device} 에서 {seconds}초 녹화 중...")
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        print("\n녹화 실패:")
        for line in tail:
            print("   ", line)
        print("\n카메라를 guard.py 가 잡고 있으면 먼저 끄거나 --from-live 를 써.")
        return []
    return sorted(out_dir.glob("seg*.ts"))


def main():
    ap = argparse.ArgumentParser(description="motion threshold calibration")
    ap.add_argument("--seconds", type=int, default=30)
    ap.add_argument("--from-live", action="store_true",
                    help="analyse live/*.ts from a running guard.py")
    ap.add_argument("--no-faces", action="store_true")
    args = ap.parse_args()

    cfg = make_cfg()
    guard.load_dotenv(guard.Path(guard.__file__).resolve().parent / ".env")
    for key in ("device", "ffmpeg", "root", "size", "fps", "input_codec"):
        setattr(cfg, key, guard.env_default(key, getattr(cfg, key)))

    tmp = None
    if args.from_live:
        segs = sorted((Path(cfg.root) / "live").glob("seg*.ts"))
        if not segs:
            sys.exit(f"{Path(cfg.root) / 'live'} 에 세그먼트가 없어. guard.py 가 돌고 있어?")
        # The newest is still being written, same rule the analyzer follows.
        segs = segs[:-1]
    else:
        if not cfg.device:
            sys.exit("카메라 이름이 없어. .env 의 GUARD_DEVICE 를 채워.")
        tmp = Path(tempfile.mkdtemp(prefix="calibrate-"))
        segs = record_sample(cfg, tmp, args.seconds)
        if not segs:
            shutil.rmtree(tmp, ignore_errors=True)
            sys.exit(1)

    want_faces = not args.no_faces
    print(f"\n세그먼트 {len(segs)}개 · 현재 임계값 {cfg.motion_threshold}%\n")
    print(f"{'세그먼트':<14}{'변화 %':>9}{'얼굴':>7}   {'판정':<8}")
    print("-" * 46)

    scores = []
    for p in segs:
        frames = guard.sample_frames(cfg, p, guard.MOTION_W, guard.MOTION_H,
                                     guard.MOTION_FPS)
        if len(frames) < 2:
            continue
        score = guard.motion_score(frames)
        faces = guard.count_faces(cfg, p) if want_faces else 0
        scores.append(score)
        fires = score >= cfg.motion_threshold or faces > 0
        print(f"{p.name:<14}{score:>9.2f}{faces:>7}   "
              f"{'기록됨' if fires else '조용함':<8}")

    if scores:
        scores.sort()
        floor = scores[len(scores) // 2]
        print("-" * 46)
        print(f"최소 {scores[0]:.2f}   중앙값 {floor:.2f}   최대 {scores[-1]:.2f}")
        print(f"\n빈 방에서 측정했다면 중앙값({floor:.2f})이 노이즈 바닥이야.")
        print(f"임계값은 그보다 확실히 위, 실제 움직임보다는 아래로 잡아:")
        print(f"  .env 에  GUARD_MOTION_THRESHOLD={max(0.3, floor * 2 + 0.5):.1f}")

    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
