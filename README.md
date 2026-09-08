# webcam-guard

**Turn a spare USB webcam into a room monitor.** Windows only. Records around the clock, and lets you watch the live picture and past footage from a phone browser.

*[한국어 문서는 아래에 있습니다 ↓](#한국어)*

Built by hand instead of using Frigate or motionEye. Only three things were needed, those tools wanted Docker and config files for it, and — more importantly — the footage should never leave the machine it was recorded on. Here it doesn't.

```
one ffmpeg process ──┬── clips/   10-minute mp4 archive
  (owns the webcam)  └── live/    HLS live stream
                           │              │
                           │              └─> motion + face analysis
                           ↓                        ↓
             guard.py (watchdog · cleanup · HTTP)  events/  logbook
                           ↑
                    phone browser
```

## What it does

- **Continuous recording** — split into 10-minute mp4 files, so a power cut costs you ten minutes, not the day
- **Live view from a phone** — browser only, no app. A 24-hour timeline lets you jump to any moment
- **Automatic cleanup** — deletes the oldest footage once it passes your age or size limit
- **Clock burnt into the picture** — so a clip is self-evidently from a particular moment
- **Logbook** — records only the stretches where something moved or a face appeared, so you can skip the empty hours

## What it deliberately does not do

- **Audio** (`-an`) — recording conversation carries legal weight that varies by jurisdiction
- **Alerts / push** — detections are written down, not pushed. Notifications with false positives get muted within days, and after that neither the alerts nor the logbook are trusted
- **Face recognition that identifies people** — it only answers "is there a face". Storing who someone is changes the nature of the data this program holds
- **Cloud upload** — recordings stay on the machine

## Design constraints

- **Zero pip packages.** Python standard library plus the ffmpeg executable. `python guard.py` has to be the whole story
- **The server is one file** (`guard.py`), **the viewer is one file** (`index.html`)
- **Plain HTTP, no encryption.** Which is why this README pushes [Tailscale](#4-viewing-from-outside--tailscale-recommended) rather than port forwarding

## Requirements

Windows 10/11 · Python 3.10+ · ffmpeg · a USB webcam

## Quick start

```powershell
winget install Gyan.FFmpeg     # 1. install ffmpeg, then open a NEW PowerShell
python guard.py --list         # 2. find your camera's name
python tools/vendor.py         # 3. fetch viewer assets (once)
copy .env.example .env         # 4. put your password and camera name in it
python guard.py                # 5. run
```

Then open `http://localhost:8088` (username `admin`). Details below.

## Layout

```
webcam-guard\
  ├─ guard.py                  ← the server; this one file is all of it
  ├─ index.html                ← the phone viewer
  ├─ .env.example              ← copy to .env (password, camera name)
  ├─ static\                   ← hls.js and fonts used by the viewer (see 3-1)
  ├─ start_guard.bat.example   ← copy to start_guard.bat for auto-start
  ├─ README.md  PROJECT.md  CHANGELOG.md  CLAUDE.md
  └─ tools\                    ← development and verification; not needed to run
```

`guard.py` and `index.html` must sit in the **same folder**.

Recordings accumulate separately, under `--root` (default `C:\CamRecordings`):

```
C:\CamRecordings\
  ├─ clips\    10-minute mp4 files
  ├─ live\     scratch files for the live stream (rotating, safe to delete)
  └─ events\   the logbook. One file per day, a few KB
```

## Verification status

Recording, live view and browser playback were confirmed on real hardware: Windows 11, ffmpeg 9.0, a Logitech C920.
`python tools/selftest.py` re-checks 54 items without needing a camera. What remains unverified is listed in [PROJECT.md](PROJECT.md) section 2.

## Documents

- **[PROJECT.md](PROJECT.md)** — design intent, verification status, "do not touch" list. Read before changing code
- **[CHANGELOG.md](CHANGELOG.md)** — what changed and why, including decisions that measurement overturned
- **[CLAUDE.md](CLAUDE.md)** — condensed rules for AI agents

---

# Setup and operation

From here on, follow in order for a first install.

## 1. Install ffmpeg

In PowerShell:

```powershell
winget install Gyan.FFmpeg
```

Open a **new** PowerShell window afterwards and check that `ffmpeg -version` responds.
(If it doesn't, point at the binary directly with `--ffmpeg "C:\path\to\ffmpeg.exe"`.)

## 2. Find the camera name

```powershell
python guard.py --list
```

In the output, copy the name **inside the quotes** on a line like `[dshow @ ...] "HD Webcam"`.

> **Why a name and not an index**: Windows identifies webcams by DirectShow device name. Index numbers shift when USB devices are replugged; the name does not, which matters for something meant to run for months.

## 3. Run

Copy `.env.example` to **`.env`** and fill in two lines:

```ini
GUARD_PASSWORD=your-own-password
GUARD_DEVICE=HD Webcam
```

Then:

```powershell
python guard.py
```

Open `http://localhost:8088` → username `admin`, password as set above.

> **Why `.env` and not `--password`**: a command-line argument is visible to every other process on the machine — Task Manager's command line column, `wmic process get commandline`, shell history. `.env` stays in the file, and it is in `.gitignore` so it cannot be committed by accident.
>
> No `python-dotenv` involved. Parsing `KEY=value` needs about twenty lines of standard library, and adding a package would break the one-command install this project is built around.

**Precedence is `CLI flag` > `environment (including .env)` > `default`.** Keep everyday values in `.env` and override for one-offs:

```powershell
python guard.py --port 9000        # just this once, on port 9000
```

Every setting works this way. Prefix the name with `GUARD_` and upper-case it (`--retain-days` → `GUARD_RETAIN_DAYS`). The full list is in `.env.example`.

### 3-1. If it will run without internet (recommended)

```powershell
python tools/vendor.py
```

This downloads hls.js and the fonts the viewer uses into `static\`. **Once** is enough.

> **Why it matters**: without it the viewer fetches those from a CDN. When the recording machine cannot reach the internet — a line fault, a Tailscale-only setup — this is the usual reason **the live picture fails on Android Chrome**. With the files vendored, the viewer makes zero external requests. (It still works without them, via CDN fallback.)

### Main options

| Option | Default | Meaning |
|---|---|---|
| `--root` | `C:\CamRecordings` | Where recordings go |
| `--size` / `--fps` | `1280x720` / `15` | Resolution, frames per second |
| `--bitrate` | `1500k` | Quality. **Measured 15.4 GB/day** — this is what decides disk usage |
| `--max-gb` | `200` | Delete oldest past this size (the guardrail) |
| `--retain-days` | `10` | Delete past this age (the actual retention policy) |
| `--segment-seconds` | `600` | Length of one file (10 minutes) |
| `--port` | `8088` | Viewer port |
| `--user` | `admin` | Viewer login name |
| `--channel` | `ROOM 01` | Name shown at the top of the viewer; useful with several cameras |
| `--no-timestamp` | — | Turn off the burnt-in clock |
| `--input-codec mjpeg` | — | For low frame rates or input failures |

Logbook options are in [3-2](#3-2-logbook--motion-and-faces). For everything: `python guard.py --help`.

> **Why 10-minute files**: recording a whole day into one file means a power cut or forced shutdown corrupts all of it. Segmented, you lose the last ten minutes. Finding a particular time is also far easier.

> **Why `--max-gb` matters**: 720p at 15fps still accumulates over 15 GB a day (measured 15.4). Without automatic deletion the system drive fills within days and Windows itself stops working. Set it below your free space.

> **Lowering `--fps` does not save disk.** File size is set by `--bitrate`. At 10fps and 1500k the daily total is nearly the same — the same bits are simply spread over fewer frames, so quality goes up. To use less disk, lower `--bitrate`. `--fps` is the CPU knob.

> **`--max-gb` and `--retain-days`: whichever hits first wins.** The defaults are tuned for **10 days** — 15.4 GB × 10 ≈ 154 GB, and `--max-gb 200` sits comfortably above that, so normally the age limit is what applies. The size cap is there for the exceptional case, like a bitrate spike.
>
> To keep footage longer, **raise both**. For 20 days: `--retain-days 20 --max-gb 380`. Raising only `--retain-days` means the size cap applies first and nothing actually changes.

---

## 3-2. Logbook — motion and faces

**Run this for days and most of the time nothing happens.** The logbook records only the stretches where something did, so you don't have to scrub through hours of an empty room.

It is on by default. Look under the viewer's **Archive** tab:

- The 24-hour bar has **two lanes**. The top one is recorded footage, the bottom one is detections
- In the bottom lane, **lighter is motion, solid is a face**
- Clicking a row or a mark **jumps straight to that moment**

Entries are also plain files, one per day, at `C:\CamRecordings\events\2026-08-24.jsonl`. One JSON object per line — readable in any editor, and easy to feed to something else:

```json
{"start": "10:39:12", "end": "10:39:48", "duration": 36, "kind": "face", "faces": 1, "score": 88.6}
```

### Tuning sensitivity

Lighting and background differ per room, so the default is not always right. Measuring beats guessing:

```powershell
python tools/calibrate.py
```

**Run it twice.** Once with the room empty (every line should read near 0 — that is your noise floor), once while walking through the frame. Pick a threshold between the two, closer to the floor:

```ini
GUARD_MOTION_THRESHOLD=1.0
```

If guard.py currently holds the camera, either stop it or pass `--from-live` to analyse the segments it is already writing.

| Option | Default | Meaning |
|---|---|---|
| `--no-logbook` | — | Turn the logbook off entirely |
| `--motion-threshold` | `1.0` | Percent of the picture that must change to count as motion |
| `--no-faces` | — | Motion only, no face detection |
| `--event-gap` | `10` | Seconds of quiet before an event is considered finished |

> **Why face detection needs OpenCV**: ffmpeg has no face detection. Its `dnn_detect` filter requires a backend compiled in that general-purpose builds do not ship. So OpenCV is used when present, and **when it is absent the logbook records motion only and keeps running.** guard.py itself still needs no pip packages. For face detection:
>
> ```powershell
> pip install opencv-python
> ```

> **Why no alerts**: notifications that include false positives get muted within days, and from then on neither the alerts nor the logbook are trusted. Writing things down means you check when you want to, and a wrong entry costs nothing to skip.

> **CPU cost**: measured at 50 ms for motion plus 78 ms for faces per 2-second segment — about **6% of one core**. Far less than the encoding already running. `--no-faces` more than halves it.

---

## 4. Viewing from outside — Tailscale recommended

**Do not port-forward.** This server speaks plain HTTP, so the password crosses the network in the clear, and cameras exposed to the internet are found by scanning bots in practice.

Instead, with a free [Tailscale](https://tailscale.com) account:

1. Install Tailscale on both the PC and the phone, sign in with the same account
2. Note the PC's Tailscale IP (looks like `100.x.x.x`)
3. Open `http://100.x.x.x:8088` on the phone

> **Why a VPN**: it builds a private network containing just those two devices, so no router configuration is involved and the port is not visible from outside at all. The traffic is encrypted too.

For viewing only within the same home Wi-Fi, the PC's local IP (the IPv4 address from `ipconfig`) is enough — just allow the port inbound in Windows Firewall.

---

## 5. Starting automatically at boot

Copy `start_guard.bat.example` to **`start_guard.bat`**. Nothing inside needs editing — the camera name and password come from `.env`.

`Win+R` → `shell:startup` → put a **shortcut** to that .bat there, and it runs at login.

> **What is in it**: a loop that restarts guard.py ten seconds after it exits, in case of a Python-level crash. ffmpeg dying is already handled by the watchdog inside guard.py, and if guard.py dies, ffmpeg is cleaned up with it.

> **Why a startup item and not a Windows service**: webcams are only reachable from a logged-in user session. Registered as a service, the process runs in session 0 and frequently cannot open the camera at all.

In power settings, **turn sleep off** (Control Panel → Power Options → never sleep). Recording stops when the machine sleeps.

---

## 6. Worth knowing

- **Audio is deliberately not recorded.** Recording conversation raises legal and privacy questions, so it is disabled with `-an`. Look into your local rules before enabling it.
- The live picture is HLS, so it runs **3–6 seconds behind**. That is normal; reducing it further requires WebRTC and a much more complex design.
- With `tools/vendor.py` run, the viewer uses **no** external internet. Without it, only hls.js comes from a CDN (iPhone Safari does not even need that).
- If another application (Zoom, Teams) holds the webcam, ffmpeg cannot open it. A restart counter climbing in the status panel is the sign.
- **Logbook entries in `events/` are never deleted automatically.** They are a few KB per day, so years are fine. But clips disappear after 10 days, so an old entry may point at footage that no longer exists — the viewer says so rather than failing.
- **Nothing is uploaded anywhere.** Analysis happens on the machine, and the logbook stores times and counts, never images.

## Troubleshooting

| Symptom | Check |
|---|---|
| `Could not run graph` / input failure | Another app holds the camera, or `--size` is a mode the camera does not support |
| **Restart counter keeps climbing** | Check whether Zoom/Teams is using the camera. Orphaned ffmpeg used to be the main cause; that is now prevented in code |
| Choppy frames | Add `--input-codec mjpeg`, or drop to `--size 640x480` |
| `drawtext` error | Run with `--no-timestamp` (font path problem) |
| Live picture missing on Android only | `tools/vendor.py` was not run and the PC cannot reach the internet |
| CPU at 100% | `--preset` is already veryfast — lower `--fps 10`, `--size 640x480`. `--no-faces` also helps |
| **Logbook stays empty** | Too insensitive. Run `python tools/calibrate.py --from-live` to see real values, then lower `GUARD_MOTION_THRESHOLD` |
| **Empty room, logbook full of entries** | Too sensitive. Check for a TV, monitor or moving branches in frame, and raise the threshold |
| **No faces ever detected** | OpenCV is missing. The status panel shows `(motion)` next to today's count when this is the case. `pip install opencv-python` |
| One event every time it starts | Expected. The camera's auto-exposure settles during the first two seconds, which changes the whole picture |

### Orphaned ffmpeg (fixed)

This program used to have a trap: **killing it from Task Manager left ffmpeg alive**, holding the camera, after which restarting could not open the device and the restart counter just climbed.

ffmpeg is now tied to a Windows Job Object, so **however guard.py dies, ffmpeg is cleaned up with it.** Confirmed by actually force-killing it. Any way of stopping it is safe.

If an older version left something behind, once:

```powershell
taskkill /F /IM ffmpeg.exe
```

---
---

<a name="한국어"></a>

# webcam-guard (한국어)

**남는 USB 웹캠을 방 감시 카메라로 만드는 프로그램.** Windows 전용. 24시간 상시 녹화하고, 휴대폰 브라우저로 실시간 화면과 지난 녹화를 본다.

Frigate·motionEye 같은 기성품 대신 직접 만들었다. 필요한 기능이 셋뿐인데 그것들은 Docker와 설정 파일을 요구했고, 무엇보다 **녹화물이 기록된 기기 밖으로 나가지 않아야** 했다.

```
ffmpeg 1개 프로세스 ──┬── clips/   10분짜리 mp4 아카이브
   (웹캠을 점유)      └── live/    HLS 실시간 스트림
                            │              │
                            │              └─> 움직임·얼굴 분석
                            ↓                        ↓
              guard.py (감시 · 자동삭제 · HTTP)   events/  로그북
                            ↑
                    휴대폰 브라우저
```

## 하는 일

- **24시간 상시 녹화** — 10분 단위 mp4로 쪼개 저장한다. 정전이 나도 잃는 건 10분이지 하루가 아니다
- **휴대폰에서 실시간 보기** — 앱 없이 브라우저만으로. 24시간 타임라인에서 원하는 시각으로 바로 이동한다
- **자동 삭제** — 설정한 기간·용량을 넘으면 오래된 것부터 지운다
- **화면에 시각 표시** — 영상 자체에 시계가 찍혀 언제 것인지가 분명하다
- **로그북** — 움직임이나 얼굴이 잡힌 구간만 적어둔다. 아무 일도 없던 시간을 건너뛸 수 있다

## 일부러 안 하는 일

- **오디오 녹음** (`-an`) — 대화 녹음은 지역에 따라 법적 문제가 된다
- **알림·푸시** — 감지한 건 적어두기만 한다. 오탐이 섞인 알림은 며칠이면 꺼두게 되고, 그때부터는 알림도 로그북도 신뢰를 잃는다
- **신원을 식별하는 얼굴 인식** — 얼굴이 "있다/없다"만 본다. 누구인지 저장하기 시작하면 이 프로그램이 다루는 데이터의 성격이 달라진다
- **클라우드 업로드** — 영상은 기록된 기기에만 있는다

## 설계 원칙

- **pip 패키지 0개.** 파이썬 표준 라이브러리와 ffmpeg 실행 파일만. `python guard.py` 한 줄로 끝나야 한다
- **서버는 파일 하나**(`guard.py`), **뷰어도 파일 하나**(`index.html`)
- **암호화 없는 HTTP.** 그래서 포트포워딩 대신 [Tailscale](#4-밖에서-휴대폰으로-보기--tailscale-권장)을 권한다

## 요구 사항

Windows 10/11 · Python 3.10+ · ffmpeg · USB 웹캠

## 빠른 시작

```powershell
winget install Gyan.FFmpeg     # 1. ffmpeg 설치 후 PowerShell 새 창
python guard.py --list         # 2. 카메라 이름 확인
python tools/vendor.py         # 3. 뷰어 자산 내려받기 (1회)
copy .env.example .env         # 4. 비밀번호와 카메라 이름을 채우고
python guard.py                # 5. 실행
```

브라우저에서 `http://localhost:8088` (아이디 `admin`).

## 파일 구성

```
webcam-guard\
  ├─ guard.py                  ← 서버. 이 파일 하나가 전부
  ├─ index.html                ← 휴대폰 뷰어
  ├─ .env.example              ← 복사해서 .env 로 (비밀번호·카메라 이름)
  ├─ static\                   ← 뷰어가 쓰는 hls.js·폰트 (3-1 참고)
  ├─ start_guard.bat.example   ← 복사해서 자동 실행용으로
  ├─ README.md  PROJECT.md  CHANGELOG.md  CLAUDE.md
  └─ tools\                    ← 개발·검증용. 운영에는 필요 없다
```

`guard.py`와 `index.html`은 **같은 폴더**에 있어야 한다.

녹화물은 코드와 따로 `--root`(기본 `C:\CamRecordings`)에 쌓인다:

```
C:\CamRecordings\
  ├─ clips\    10분짜리 mp4
  ├─ live\     실시간용 임시 파일 (계속 순환, 지워도 된다)
  └─ events\   로그북. 날짜별 파일, 하루 몇 KB
```

## 검증 상태

Windows 11 · ffmpeg 9.0 · Logitech C920 실기기에서 녹화·실시간·브라우저 재생까지 확인했다.
`python tools/selftest.py`가 54개 항목을 카메라 없이 재검증한다. 남은 미검증 항목은 [PROJECT.md](PROJECT.md) 2절에 있다.

## 문서

- **[PROJECT.md](PROJECT.md)** — 설계 의도, 검증 상태, "손대면 안 되는 것". 코드를 고치기 전에 읽을 것
- **[CHANGELOG.md](CHANGELOG.md)** — 무엇이 왜 바뀌었는지. 실측으로 뒤집힌 결정들
- **[CLAUDE.md](CLAUDE.md)** — AI 에이전트용 요약

---

# 설치와 운영

처음 설치할 때 순서대로 따라가면 된다.

## 1. ffmpeg 설치

```powershell
winget install Gyan.FFmpeg
```

설치 후 PowerShell 창을 **새로 열고** `ffmpeg -version`이 나오는지 확인한다.
(안 나오면 `--ffmpeg "C:\경로\ffmpeg.exe"`로 직접 지정)

## 2. 카메라 이름 찾기

```powershell
python guard.py --list
```

출력에서 `[dshow @ ...] "HD Webcam"` 같은 줄의 **따옴표 안 이름**을 그대로 복사한다.

> **Why 인덱스가 아니라 이름인가**: Windows는 웹캠을 DirectShow 장치명으로 식별한다. 인덱스 번호는 USB를 다시 꽂으면 바뀌지만 이름은 고정이라, 몇 달씩 돌리는 용도에는 이름이 안전하다.

## 3. 실행

`.env.example`을 복사해 **`.env`**로 저장하고 두 줄을 채운다:

```ini
GUARD_PASSWORD=직접정한비밀번호
GUARD_DEVICE=HD Webcam
```

그리고:

```powershell
python guard.py
```

브라우저에서 `http://localhost:8088` → 아이디 `admin`.

> **Why `--password`가 아니라 `.env`인가**: 명령줄 인자는 같은 PC의 다른 프로세스에서 그대로 보인다 — 작업 관리자의 "명령줄" 열, `wmic process get commandline`, 셸 기록. `.env`는 파일에만 있고 `.gitignore`에 등록돼 실수로 커밋될 일도 없다.
>
> `python-dotenv`는 쓰지 않는다. `KEY=값` 파싱은 표준 라이브러리로 20줄이면 되고, 패키지를 하나 넣는 순간 이 프로젝트가 지키려는 "설치 명령 한 줄" 원칙이 깨진다.

**우선순위는 `명령줄 인자` > `환경변수(.env 포함)` > `기본값`.** 평소 값은 `.env`에 두고 일회성만 인자로 덮는다:

```powershell
python guard.py --port 9000        # 이번만 9000번 포트로
```

모든 설정이 이렇게 동작한다. 이름 앞에 `GUARD_`를 붙이고 대문자로 (`--retain-days` → `GUARD_RETAIN_DAYS`). 전체 목록은 `.env.example`에 있다.

### 3-1. 인터넷 없이 쓸 거라면 (권장)

```powershell
python tools/vendor.py
```

뷰어가 쓰는 hls.js와 폰트를 `static\`에 받아둔다. **한 번만** 하면 된다.

> **Why 필요한가**: 안 하면 뷰어가 그 파일들을 CDN에서 가져온다. 감시 PC가 인터넷에 못 나가는 상황(회선 장애, Tailscale 전용 구성)에서 **안드로이드 크롬의 실시간 화면이 안 뜨는** 이유가 대부분 이것이다. 받아두면 외부 요청이 0건이 된다. (안 받아도 CDN 폴백으로 동작은 한다.)

### 주요 옵션

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `--root` | `C:\CamRecordings` | 녹화 저장 폴더 |
| `--size` / `--fps` | `1280x720` / `15` | 해상도, 초당 프레임 |
| `--bitrate` | `1500k` | 화질. **실측 하루 15.4GB** — 용량을 정하는 값 |
| `--max-gb` | `200` | 이 용량을 넘으면 오래된 것부터 삭제 (안전장치) |
| `--retain-days` | `10` | 이 일수가 지나면 삭제 (실제 보관 정책) |
| `--segment-seconds` | `600` | 파일 하나의 길이(10분) |
| `--port` | `8088` | 웹 뷰어 포트 |
| `--user` | `admin` | 뷰어 로그인 아이디 |
| `--channel` | `ROOM 01` | 뷰어 상단에 뜨는 이름. 카메라가 여러 대면 구분용 |
| `--no-timestamp` | — | 화면 속 시각 표시 끄기 |
| `--input-codec mjpeg` | — | 프레임이 낮거나 입력 실패할 때 |

로그북 옵션은 [3-2](#3-2-로그북--움직임과-얼굴)에 있다. 전체는 `python guard.py --help`.

> **Why 10분 단위로 쪼개는가**: 하루를 한 파일로 녹화하면 정전·강제종료 시 전체가 깨진다. 세그먼트 방식이면 마지막 10분만 잃는다. 특정 시각을 찾기도 훨씬 쉽다.

> **Why `--max-gb`가 중요한가**: 720p 15fps로도 하루 15GB 이상 쌓인다(실측 15.4GB). 자동 삭제가 없으면 며칠 만에 시스템 드라이브가 차고 Windows 자체가 멈춘다. 반드시 여유 공간보다 작게 잡는다.

> **`--fps`를 낮춰도 용량은 안 줄어든다.** 파일 크기를 정하는 건 `--bitrate`다. 10fps로 낮춰도 1500k면 하루 용량은 거의 같고, 같은 비트가 더 적은 프레임에 쓰이니 화질만 좋아진다. 용량을 줄이려면 `--bitrate`를 낮춘다. `--fps`는 CPU를 줄이는 옵션이다.

> **`--max-gb`와 `--retain-days`는 둘 중 먼저 걸리는 쪽이 이긴다.** 기본값은 **10일 보관**에 맞춰져 있다. 15.4GB × 10일 ≈ 154GB이고 `--max-gb 200`은 그보다 넉넉해서 평소엔 날짜 쪽이 걸린다. 용량 쪽은 비트레이트가 튀는 예외 상황을 막는 안전장치다.
>
> 보관 기간을 늘리려면 **두 값을 같이** 올린다. 20일이면 `--retain-days 20 --max-gb 380`. `--retain-days`만 올리면 용량 상한에 먼저 걸려 실제로는 안 늘어난다.

---

## 3-2. 로그북 — 움직임과 얼굴

**며칠 켜두면 대부분의 시간에는 아무 일도 없다.** 로그북은 뭔가 있었던 구간만 적어둬서, 빈 방을 몇 시간씩 넘겨보지 않아도 되게 한다.

기본으로 켜져 있고, 뷰어의 **기록** 탭에서 본다:

- 24시간 막대가 **두 줄**이다. 위는 녹화된 구간, 아래는 감지된 지점
- 아래 줄에서 **연한 것은 움직임, 진한 것은 얼굴**
- 목록이나 막대를 누르면 **그 시각으로 바로 이동**한다

기록은 `C:\CamRecordings\events\2026-08-24.jsonl` 처럼 날짜별 파일로도 남는다. 한 줄에 JSON 하나라 아무 편집기로나 열리고, 다른 도구에 그대로 넘길 수도 있다:

```json
{"start": "10:39:12", "end": "10:39:48", "duration": 36, "kind": "face", "faces": 1, "score": 88.6}
```

### 감도 조절

방마다 조명·배경이 달라 기본값이 항상 맞지는 않는다. 추측보다 측정이 낫다:

```powershell
python tools/calibrate.py
```

**두 번 돌린다.** 방을 비우고 한 번(전부 0에 가깝게 나와야 정상 — 그게 노이즈 바닥이다), 앞을 지나다니며 한 번. 두 값 사이에서 바닥 쪽에 가깝게 정한다:

```ini
GUARD_MOTION_THRESHOLD=1.0
```

guard.py가 카메라를 쓰는 중이면 녹화를 멈추거나 `--from-live`로 이미 기록 중인 세그먼트를 분석한다.

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `--no-logbook` | — | 로그북을 아예 끔 |
| `--motion-threshold` | `1.0` | 화면의 몇 %가 바뀌어야 움직임으로 볼지 |
| `--no-faces` | — | 얼굴 감지만 끔 (움직임은 계속 기록) |
| `--event-gap` | `10` | 몇 초 조용하면 한 사건이 끝난 걸로 볼지 |

> **Why 얼굴 감지에 OpenCV가 필요한가**: ffmpeg에는 얼굴 감지가 없다. `dnn_detect` 필터는 별도 백엔드를 넣어 빌드해야 하는데 일반 배포판에는 없다. 그래서 OpenCV가 있으면 쓰고, **없으면 움직임만 기록하고 그대로 계속 돈다.** guard.py 자체는 여전히 pip 패키지가 필요 없다. 얼굴 감지까지 원하면:
>
> ```powershell
> pip install opencv-python
> ```

> **Why 알림을 안 보내나**: 오탐이 섞인 알림은 며칠이면 무시하게 되고, 그때부터는 알림도 로그북도 안 믿게 된다. 적어두기만 하면 필요할 때 확인하고, 틀린 항목은 그냥 넘기면 된다.

> **CPU는 얼마나 쓰나**: 2초 세그먼트당 모션 50ms + 얼굴 78ms, 합쳐서 **코어 하나의 약 6%**(실측). 이미 돌아가는 인코딩보다 훨씬 적다. `--no-faces`면 절반 이하가 된다.

---

## 4. 밖에서 휴대폰으로 보기 — Tailscale 권장

**공유기 포트포워딩은 하지 않는다.** 이 서버는 평문 HTTP라 비밀번호가 그대로 지나가고, 인터넷에 열린 카메라는 실제로 스캐닝 봇에게 발견된다.

대신 [Tailscale](https://tailscale.com) 무료 계정으로:

1. PC와 휴대폰 양쪽에 설치하고 같은 계정으로 로그인
2. PC의 Tailscale IP 확인 (`100.x.x.x` 형태)
3. 휴대폰 브라우저에서 `http://100.x.x.x:8088`

> **Why VPN 방식인가**: 두 기기만 있는 사설망을 만들어 주므로 공유기 설정을 건드릴 필요가 없고, 외부에서는 포트 자체가 보이지 않는다. 트래픽도 암호화된다.

같은 집 와이파이 안에서만 볼 거라면 PC의 내부 IP(`ipconfig`의 IPv4)로 접속하고, Windows 방화벽에서 해당 포트 인바운드만 허용하면 된다.

---

## 5. 부팅 시 자동 실행

`start_guard.bat.example`을 복사해 **`start_guard.bat`**으로 저장하면 끝이다. 안을 고칠 필요는 없다 — 카메라 이름과 비밀번호는 `.env`에서 읽는다.

`Win+R` → `shell:startup` → 이 .bat의 **바로가기**를 넣으면 로그인 시 실행된다.

> **무엇이 들었나**: guard.py가 파이썬 예외로 죽었을 때 10초 뒤 다시 띄우는 루프. ffmpeg이 죽는 건 guard.py 안의 워치독이 처리하고, guard.py가 죽으면 ffmpeg도 같이 정리된다.

> **Why 서비스가 아니라 시작프로그램인가**: 웹캠은 로그인한 사용자 세션에서만 접근된다. 서비스로 등록하면 세션 0에서 돌아 카메라를 못 잡는 경우가 많다.

전원 설정에서 **절전을 꺼둔다** (제어판 → 전원 옵션 → 절전 안 함). 잠들면 녹화가 멈춘다.

---

## 6. 알아둘 것

- **오디오는 일부러 녹음하지 않는다.** 법적·프라이버시 문제가 생길 수 있어 `-an`으로 꺼뒀다. 켤 거라면 지역 규정을 먼저 확인한다.
- 실시간 화면은 HLS라 **3~6초 지연**이 있다. 정상이며, 더 줄이려면 WebRTC가 필요하고 구조가 훨씬 복잡해진다.
- `tools/vendor.py`를 돌려두면 뷰어가 외부 인터넷을 **전혀** 쓰지 않는다. 안 돌렸다면 hls.js만 CDN에서 가져온다(아이폰 Safari는 그것도 필요 없다).
- 다른 앱(줌, 팀즈)이 웹캠을 쓰면 ffmpeg이 장치를 못 연다. 상태창의 재시작 횟수가 계속 오르면 이것을 의심한다.
- **로그북 기록(`events/`)은 자동으로 지워지지 않는다.** 하루 몇 KB라 몇 년을 둬도 괜찮다. 다만 클립은 10일 뒤 사라지므로 오래된 기록이 없는 영상을 가리킬 수 있다 — 뷰어가 그렇다고 알려준다.
- **아무 데도 업로드하지 않는다.** 분석도 기기 안에서 끝나고, 로그북에는 시각·종류·개수만 남지 이미지는 저장하지 않는다.

## 문제 해결

| 증상 | 확인할 것 |
|---|---|
| `Could not run graph` / 입력 실패 | 다른 앱이 카메라 점유 중이거나, `--size`를 카메라가 지원하지 않음 |
| **재시작 횟수만 계속 오름** | 줌·팀즈가 카메라를 쓰는지 확인. 예전엔 고아 ffmpeg이 주원인이었으나 지금은 코드에서 막았다 |
| 프레임이 뚝뚝 끊김 | `--input-codec mjpeg` 추가, 또는 `--size 640x480` |
| `drawtext` 오류 | `--no-timestamp`로 실행 (폰트 경로 문제) |
| 안드로이드에서만 실시간이 안 나옴 | `tools/vendor.py`를 안 돌렸고 PC가 인터넷에 못 나가는 경우 |
| CPU 100% | `--preset`은 이미 veryfast — `--fps 10`, `--size 640x480`으로. `--no-faces`도 도움이 된다 |
| **로그북이 계속 비어 있음** | 너무 둔한 것. `python tools/calibrate.py --from-live`로 실제 값을 보고 `GUARD_MOTION_THRESHOLD`를 낮춘다 |
| **빈 방인데 기록이 잔뜩** | 너무 예민한 것. 화면에 TV·모니터·흔들리는 나뭇가지가 있는지 보고 임계값을 올린다 |
| **얼굴이 하나도 안 잡힘** | OpenCV가 없는 상태. 상태창의 오늘 감지 옆에 `(모션)`이 뜬다. `pip install opencv-python` |
| 기동할 때마다 사건이 하나 생김 | 정상이다. 카메라 자동노출이 잡히는 첫 2초는 화면 전체가 변한다 |

### 고아 ffmpeg (해결됨)

원래 이 프로그램에는 함정이 있었다. **작업 관리자로 강제 종료하면 ffmpeg만 살아남아** 카메라를 붙잡고, 그 뒤엔 다시 켜도 장치를 못 열어 재시작 카운터만 올라갔다.

지금은 Windows Job Object에 묶여 있어 **guard.py가 어떻게 죽든 ffmpeg도 같이 정리된다.** 강제 종료로 실제 확인했다. 어떻게 꺼도 괜찮다.

예전 버전의 흔적이 남아 증상이 보이면 한 번만:

```powershell
taskkill /F /IM ffmpeg.exe
```
