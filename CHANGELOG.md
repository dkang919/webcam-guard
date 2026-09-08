# CHANGELOG

What changed, when, and **why it ended up that way**.

[PROJECT.md](PROJECT.md) section 5 holds the final reasoning; this file records **how we got there**, and in particular the decisions that were *reversed*. Explaining why an idea does not work needs the record of it having been tried.

Dates are when the work was actually verified.

*[한국어는 아래에 있습니다 ↓](#한국어-1)*

---

## 2026-08-24 — Logbook (motion and faces)

**Problem**: most of a 24-hour recording is nothing happening. Finding anything meant scrubbing from the start.

**Decision**: keep recording continuously, but index the stretches where something occurred. `events/<date>.jsonl` plus a second lane on the viewer's timeline.

| What | Why |
|---|---|
| Analyse `live/*.ts` (2-second segments) | Reopening the camera is impossible on Windows; 10-minute clips mean 10 minutes of latency and a heavy decode. **Reading a file is unrelated to the "one ffmpeg" rule** |
| Merge events into intervals | One line per segment means someone present for a minute produces 30 lines — an unreadable logbook |
| Resolve the clip at **read** time | When an event happens, that clip is still being recorded |
| Face detection **optional**, via OpenCV | ffmpeg has no face detection (`dnn_detect` needs a backend general builds don't ship). Absent OpenCV, motion is still recorded and the program keeps running |

### Two decisions that measurement reversed

**① Motion metric: mean brightness difference → percentage of changed pixels**

The first implementation used mean absolute difference between frames. It looked plausible against synthetic video, but **a full-screen moving test pattern scored only 4.23**. With a threshold of 6.0, a person walking through would have recorded nothing at all. Averaging dilutes a small subject.

Counting changed pixels separated cleanly: an empty room reads **0.00%**, a person moving reads 8–90%. The value is also "percent of the picture", which makes it tunable by hand.

**② Face detection: gated behind motion → always on**

To save CPU, faces were only checked when motion fired. That **misses someone sitting still entirely** — and the requirement was "a face is detected **or** motion is detected". Measured cost is 78 ms per 2-second segment (about 4% of a core), which is affordable to run unconditionally.

> The burnt-in clock was assumed to trigger false motion, so the bottom strip was cropped out. Measurement showed the assumption was wrong: at 64×36 the text blurs away and the score is 0.00 with or without the crop. The crop stayed anyway — at higher resolution or font size it would genuinely matter.

### Bugs fixed along the way

- **Duplicate viewer marks** — `loadEvents()` appends to the strip after an async fetch; if another render cleared the strip in between, two sets accumulated (28 marks against a 14-row list). A render sequence number now discards stale responses. Reproduced by clicking day chips rapidly.
- **UTF-8 crash, again** — the previous fix was placed inside `main()`, which was wrong. The analyzer thread and everything under `tools/` never go through `main()` and died on a single Korean log line. Module import time was the correct scope.
- **`_scratch` deletion** — `sample_clip_bytes()` used `_scratch` as its temp directory and then removed it recursively, taking the dev server's recordings folder with it. Invisible until ffmpeg was installed, because it returned early without one.

**Added**: `tools/calibrate.py` — lighting and background differ per room, so the default is not universally right. Run it twice, empty room and occupied, and pick a threshold between the two.

selftest 37 → **54 items**.

---

## 2026-08-24 — Live-file race, UTF-8 crash

**Problem**: `PermissionError: [Errno 13] ... live.m3u8` traceback during normal operation.

ffmpeg **truncates and rewrites** `live.m3u8` every couple of seconds. Serving that file with `stat()` → open → stream breaks three ways:

| Symptom | Exception raised |
|---|---|
| Sharing violation (Windows refuses the open) | `PermissionError` |
| `stat()` size ≠ bytes actually read | none → browser `ERR_CONTENT_LENGTH_MISMATCH` |
| Read after truncate, before rewrite | **none** → half-written playlist |

The third was the dangerous one: nothing raises, so wrong content is served silently. **Without writing the regression test first, only the first symptom would have been fixed.**

**Fix**: `send_live()` reads one snapshot into memory and, for `.m3u8`, validates the content (starts with `#EXTM3U`, ends with a newline). On failure it retries six times at 30 ms, then answers `503` with `Retry-After`.

> ffmpeg's `hls_flags=+temp_file` would make the swap atomic, but it was **not** used: if a reader holds the file open, the rename fails on Windows and the HLS output breaks instead. Defending in the server keeps the risk on one side.

**Measured**: 12 concurrent threads for 45 seconds — server exceptions 1 → **0**. With keep-alive, 78242 requests all clean.

**Also fixed**: with output redirected, stdout falls back to the legacy code page and **a single Korean log line killed the process**. Recording could not even start, which made it more severe than the reported bug.

---

## 2026-08-24 — `.env` configuration

**Problem**: a password passed as `--password` stays in the process command line, where any other process on the machine can read it (Task Manager, `wmic process get commandline`, shell history).

**Decision**: a `.env` file plus `GUARD_<KEY>` environment variables. Precedence is **CLI flag > environment (.env) > default**.

`.env` means "the usual value"; a flag means "different just this once". Reversed, `--port 9000` would be silently ignored. The implementation feeds `.env` values in as argparse *defaults*, so there is no need to track whether a flag was explicitly given.

`python-dotenv` was not used. Parsing `KEY=value` is twenty lines of standard library, and one package breaks the one-command install.

Details: a real environment variable beats `.env` (a one-off should not lose to a file) · the BOM Notepad writes is stripped (otherwise it becomes part of the first key name and silently does nothing) · `bool` is checked before `int`, since in Python `bool` is a subclass of `int`.

---

## 2026-08-23 — Initial commit, verification on real hardware

First run against real hardware: Windows 11, ffmpeg 9.0, a **Logitech C920**. Everything before that had been synthetic input in a Linux container, which is why three problems surfaced at once.

- **drawtext escaping** — `C\:/...` (one backslash) fails on ffmpeg 9.0. Not just the overlay: **ffmpeg died outright and nothing was recorded at all.** `C\\:/...` (two) is correct. The filter is now dropped when no font is found, because losing all footage to a decoration is the worst possible trade.
- **Orphaned ffmpeg** — force-killing the parent left ffmpeg alive, holding the camera permanently. Solved by tying it to a Windows Job Object (`KILL_ON_JOB_CLOSE`).
- **UTF-8 device names** — ffmpeg's stderr was decoded with the locale code page, mangling non-ASCII device names.

**Measured**: 720p / 15fps / 1500k produces **15.4 GB per day**. Retention defaults were set from that number: 10 days and 200 GB (whichever hits first wins, so both must be raised together).

**Added**: `tools/` — verification and development without a camera. `guard.py`'s input half was split into `build_input_args()` so the **output half stays real code** while only the input is swapped for a synthetic source. The output half is where things actually break, so mocking it would make the test meaningless.

**Also**: hls.js and IBM Plex vendored into `static/` (zero external requests from the viewer) · `.gitignore` blocking recordings and password-bearing files · `start_guard.bat.example`.

---
---

<a name="한국어-1"></a>

# CHANGELOG (한국어)

무엇이 언제 바뀌었고 **왜 그렇게 됐는지**의 기록.

설계 근거는 [PROJECT.md](PROJECT.md) 5절이 최종본이고, 이 문서는 **거기까지 어떻게 도달했는지**를 남긴다. 특히 *뒤집힌 결정*을 적어둔다 — 어떤 아이디어가 왜 안 되는지 설명하려면 그것을 시도해 본 기록이 필요하다.

날짜는 실제 검증을 수행한 날이다.

---

## 2026-08-24 — 로그북 (움직임·얼굴)

**문제**: 24시간 녹화의 대부분은 아무 일도 없다. 볼 곳을 찾으려면 처음부터 넘겨봐야 했다.

**결정**: 상시 녹화는 그대로 두고, 사건이 있던 구간만 색인한다. `events/<날짜>.jsonl` + 뷰어 타임라인의 두 번째 레인.

| 무엇 | 왜 |
|---|---|
| `live/*.ts`(2초 세그먼트)를 분석 | 카메라 재점유는 Windows에서 불가능, 10분 클립은 지연·비용 둘 다 크다. **파일 읽기는 "ffmpeg 하나" 규칙과 무관** |
| 사건을 구간으로 병합 | 세그먼트마다 한 줄이면 1분 머문 사람이 30줄. 읽을 수 없는 로그북이 된다 |
| 클립 연결은 **읽는 시점**에 | 사건이 난 순간 그 클립은 아직 녹화 중이다 |
| 얼굴 감지는 OpenCV **선택 사용** | ffmpeg에 얼굴 감지가 없다(`dnn_detect`는 별도 백엔드 빌드 필요, 일반 배포판엔 없음). 없으면 모션만 기록하고 계속 돈다 |

### 실측으로 뒤집힌 결정 둘

**① 모션 지표: 평균 밝기차 → 변한 픽셀의 비율**

처음 구현은 프레임 간 평균 절대차였다. 합성 영상으로는 그럴듯했는데, **화면 전체가 움직이는 테스트 패턴조차 4.23**이 나왔다. 임계값 6.0으로는 사람이 지나가도 아무것도 기록되지 않았을 것이다. 평균은 작은 피사체를 희석시킨다.

변한 픽셀 비율로 바꾸니 빈 방 **0.00%**, 사람 움직임 8~90%로 분리됐다. 값이 "화면의 몇 %"라 손으로 튜닝하기도 쉽다.

**② 얼굴 검사: 모션 뒤에 게이팅 → 항상 실행**

CPU를 아끼려 모션이 잡혔을 때만 검사했는데, **가만히 앉아 있는 사람을 통째로 놓친다.** 요구사항은 "얼굴이 인식되거나 **또는** 모션이 잡히면"이었다. 실측 비용이 2초당 78ms(코어의 4%)라 항상 돌려도 감당된다.

> 화면에 찍히는 시계가 모션으로 오탐될 거라 보고 하단을 크롭했는데, 실측하니 64×36에서는 글자가 뭉개져 **크롭 없이도 0.00**이었다. 전제가 틀렸지만 크롭은 남겨뒀다 — 해상도나 폰트를 키우면 그때는 실제로 문제가 된다.

### 함께 고친 버그

- **뷰어 마커 중복** — `loadEvents()`가 비동기 응답을 스트립에 덧붙이는데 그 사이 다른 렌더가 스트립을 비우면 두 벌이 쌓였다(마커 28개 vs 목록 14개). 렌더 번호로 낡은 응답을 버린다. 날짜 칩 연타로 재현했다.
- **UTF-8 크래시 재발** — 직전 수정을 `main()` 안에 넣은 게 틀렸다. 분석 스레드와 `tools/`는 `main()`을 거치지 않아 한국어 로그 한 줄에 그대로 죽었다. **모듈 로드 시점**이 맞는 위치였다.
- **`_scratch` 삭제 사고** — `sample_clip_bytes()`가 임시 폴더로 `_scratch`를 쓰고 통째로 지워서 devserver의 녹화 폴더까지 날렸다. ffmpeg이 없을 땐 일찍 반환해서 안 보이던 버그다.

**추가**: `tools/calibrate.py` — 방마다 조명·배경이 달라 기본값이 항상 맞지 않는다. 빈 방과 사람이 있는 상태로 두 번 재서 임계값을 정한다.

selftest 37 → **54개**.

---

## 2026-08-24 — 라이브 파일 경합, UTF-8 크래시

**문제**: 운영 중 `PermissionError: [Errno 13] ... live.m3u8` 트레이스백.

ffmpeg은 `live.m3u8`을 2초마다 **잘라내고 다시 쓴다.** 그 파일을 `stat()` → 열기 → 스트리밍하면 세 가지가 터진다.

| 증상 | 예외 |
|---|---|
| 공유 위반 (Windows가 열기를 거부) | `PermissionError` |
| `stat()` 크기 ≠ 실제 크기 | 없음 → 브라우저 `ERR_CONTENT_LENGTH_MISMATCH` |
| 잘라낸 뒤 다시 쓰기 전에 읽음 | **없음** → 반쪽짜리 재생목록 |

세 번째가 가장 위험했다. 예외가 나지 않아 조용히 잘못된 걸 내보낸다. **회귀 테스트를 먼저 쓰지 않았다면 1번만 고치고 끝냈을 것이다.**

**해결**: `send_live()`가 파일을 한 번에 메모리로 읽고, `.m3u8`은 내용까지 검증한다(`#EXTM3U`로 시작, 개행으로 끝). 실패 시 30ms 간격 6회 재시도, 그래도 안 되면 `503 + Retry-After`.

> ffmpeg 쪽 `hls_flags=+temp_file`로 원자적 교체를 시키는 방법은 **쓰지 않았다.** 리더가 파일을 연 상태면 Windows에서 rename이 실패해 이번엔 HLS 출력이 깨진다. 서버에서 방어하면 위험이 한쪽으로만 몰린다.

**측정**: 동시 12스레드 45초에서 서버 예외 1건 → **0건**. keep-alive 78242회 요청 전부 정상.

**함께 고친 것**: 출력을 리다이렉트하면 stdout이 레거시 코드페이지가 되어 **한국어 로그 한 줄에 프로세스 전체가 죽었다.** 녹화가 시작조차 못 하는 문제라 원래 보고된 버그보다 심각했다.

---

## 2026-08-24 — `.env` 설정

**문제**: 비밀번호를 `--password`로 넘기면 명령줄 인자에 남아 같은 PC의 다른 프로세스에서 그대로 보인다(작업 관리자, `wmic process get commandline`, 셸 기록).

**결정**: `.env` 파일 + `GUARD_<KEY>` 환경변수. 우선순위는 **CLI 인자 > 환경변수(.env) > 기본값**.

`.env`는 "평소 값", 인자는 "이번만 다르게"라는 의미다. 반대로 두면 `--port 9000`을 줘도 조용히 무시된다. 구현은 `.env` 값을 argparse의 *default*로 넣는 방식이라 "인자가 명시됐는지"를 따로 추적할 필요가 없다.

`python-dotenv`는 쓰지 않았다. `KEY=값` 파싱은 표준 라이브러리로 20줄이면 되고, 패키지 하나가 "설치 명령 한 줄" 원칙을 깬다.

세부: 실제 환경변수가 `.env`보다 우선(일회성 지정이 파일에 밀리면 이상하다) · Notepad가 붙이는 BOM 제거(안 그러면 첫 키 이름에 붙어 조용히 무시된다) · `bool` 검사를 `int`보다 먼저(파이썬에서 `bool`은 `int`의 하위 클래스).

---

## 2026-08-23 — 초기 커밋, 실기기 검증

Windows 11 · ffmpeg 9.0 · **Logitech C920**으로 처음 실제 검증했다. 그전까지는 Linux 컨테이너에서 합성 입력으로만 돌려본 상태였고, 그래서 아래 셋이 한꺼번에 드러났다.

- **drawtext 이스케이프** — `C\:/...`(백슬래시 1개)는 ffmpeg 9.0에서 실패한다. 오버레이만 빠지는 게 아니라 **ffmpeg 전체가 죽어 녹화가 아예 안 됐다.** `C\\:/...`(2개)가 맞다. 이제 폰트를 못 찾으면 필터를 빼고 녹화는 계속한다 — 장식 때문에 영상 전부를 잃는 건 최악의 트레이드다.
- **고아 ffmpeg** — 강제 종료하면 ffmpeg만 살아남아 카메라를 영구 점유했다. Windows Job Object(`KILL_ON_JOB_CLOSE`)에 묶어 해결.
- **UTF-8 장치명** — ffmpeg stderr을 로캘 코드페이지로 읽어 비ASCII 장치명이 깨졌다.

**실측**: 720p·15fps·1500k에서 **하루 15.4GB**. 이 값에 맞춰 보관 기본값을 10일 / 200GB로 정했다(둘 중 먼저 걸리는 쪽이 이기므로 함께 올려야 한다).

**추가**: `tools/` — 카메라 없이 검증·개발하는 도구. `guard.py`의 입력부를 `build_input_args()`로 분리해 **출력 절반은 진짜 코드 그대로** 두고 입력만 합성으로 갈아끼운다. 실제로 깨지는 건 항상 출력 쪽이라 그 부분을 mock 하면 검증의 의미가 없다.

**함께**: hls.js와 IBM Plex를 `static/`에 내장(뷰어의 외부 요청 0건) · `.gitignore`(녹화물·비밀번호 차단) · `start_guard.bat.example`.
