# PROJECT.md — Guard 가정용 웹캠 감시 시스템

> 이 문서는 **다음 작업자(Claude Cowork 세션 포함)를 위한 인수인계 문서**다.
> `README.md`는 최종 사용자용 설치 안내서이고, 이 파일은 설계 의도·검증 상태·수정 시 주의사항을 담는다.
> **코드를 수정하기 전에 이 문서를 끝까지 읽을 것.** 특히 "손대면 안 되는 것" 섹션.

---

## 1. 프로젝트 개요

집에 남아도는 USB 웹캠을 **집을 비운 동안 방을 감시하는 카메라**로 쓰기 위한 시스템.

| 항목 | 결정 |
|---|---|
| 실행 환경 | **Windows PC** (사용자 본인 PC, 상시 켜둠) |
| 구현 방식 | **직접 코딩** (Frigate·motionEye 같은 기성품을 의도적으로 쓰지 않음) |
| 필수 기능 | ① 24시간 상시 녹화 ② 휴대폰에서 실시간 보기 ③ 오래된 영상 자동 삭제 |
| 제외한 기능 | 모션 감지, 오디오 녹음, 얼굴/객체 인식, 알림 |
| 의존성 | ffmpeg(외부 실행 파일) + Python 표준 라이브러리만. **pip 패키지 0개** |

사용자는 한국어로 소통하며, 설명에 **"왜(Why)"를 함께 요구**한다. 코드 주석은 영어, 사용자 대면 텍스트(UI·README)는 한국어로 통일되어 있다.

---

## 2. 현재 상태 — 무엇이 검증됐고 무엇이 안 됐나

**이 구분이 가장 중요하다.** 아래 "미검증" 항목을 "동작한다"고 전제하고 작업하지 말 것.

### ✅ 실제 Windows + 실제 웹캠으로 검증됨 (2026-08-23)

검증 환경: Windows 11 Pro 26200 · Python 3.12.8 · **ffmpeg 9.0**(Gyan build, winget) · **Logitech HD Pro Webcam C920**.

- **dshow 입력** — 실제 카메라로 녹화 + HLS 동시 출력 성공. raw(yuyv422)·mjpeg 둘 다 720p 15fps 실측.
- **drawtext 타임스탬프** — 화면에 시각이 찍히는 것까지 눈으로 확인. **단, 이스케이프를 고친 뒤에야 됐다** (5-8절).
- **브라우저 HLS 재생** — Chromium에서 실시간 탭 재생, 아카이브 탭 24시간 타임라인·클립 재생 모두 확인.
- **오프라인 동작** — 뷰어 로드 시 외부 요청 **0건** (`static/` 내장 자산 사용).
- `tee` 먹서 segment(mp4) + HLS 동시 출력 / HTTP 전 라우트 / Basic 인증 / Range 206 / 경로 탈출 404.
- 실측 용량: 720p·15fps·1500k에서 **하루 15.4GB** (1080p·15fps·2500k는 26.3GB).

**전부 `python tools/selftest.py` 한 줄로 재현된다. 36개 항목 통과.** 손으로 curl 치지 말 것.

### ❌ 아직 검증 안 됨

1. **장시간 안정성** — 24시간 이상 연속 구동, janitor의 실제 삭제 동작(용량·기한 초과 상황), 워치독의 실제 재시작. 최장 연속 구동 확인은 수 분 수준이다.
2. **iOS Safari 네이티브 HLS** — 코드 경로는 있으나 실기기 미확인 (Chromium만 봤다).
3. **동시 시청 부하** — ThreadingHTTPServer에 여러 명이 붙었을 때.

> **다음 세션의 첫 할 일**: 위 1번. 하루 이상 켜두고 `/api/status`의 `restarts`와 `used_gb`가 어떻게 움직이는지 보는 것 외에 지름길이 없다.

---

## 3. 아키텍처

```
        ┌──────────────────────── guard.py (Python, 단일 프로세스) ─────────┐
        │                                                                  │
USB웹캠 │  [supervisor thread] ── subprocess ──> ffmpeg.exe                │
  │     │        재시작·백오프                      │                       │
  └─────┼───────────────────────────────────────────┤ tee muxer            │
        │                                           │                       │
        │                        ┌──────────────────┴──────────────────┐   │
        │                        ▼                                     ▼   │
        │           clips/YYYY-MM-DD_HH-MM-SS.mp4          live/live.m3u8  │
        │           (10분 단위 아카이브)                     live/segN.ts    │
        │                        │                                     │   │
        │  [janitor thread] ─────┘ 용량·기한 초과분 삭제                  │   │
        │                                                              │   │
        │  [ThreadingHTTPServer :8088] ── Basic Auth ──────────────────┘   │
        │        └─> index.html (뷰어) + JSON API + 파일 서빙                │
        └──────────────────────────────────────────────────────────────────┘
                                   ▲
                        휴대폰 브라우저 (Tailscale 경유)
```

### 왜 이 구조인가 — 핵심 제약

**Windows에서 웹캠은 한 번에 한 프로세스만 점유할 수 있다.**
그래서 "녹화용 ffmpeg 하나 + 스트리밍용 ffmpeg 하나"는 **불가능**하다. 반드시 ffmpeg 프로세스 **하나**가 카메라를 잡고, 그 안에서 `tee` 먹서로 출력을 둘로 나눠야 한다. 인코딩도 한 번만 일어나므로 CPU도 절약된다.

이 제약 때문에 다음이 자동으로 따라온다:
- 모션 감지든 뭐든 **새 기능은 반드시 기존 ffmpeg 파이프라인 안에** 넣어야 한다 (예: `select` 필터, 또는 tee에 세 번째 출력 추가).
- 카메라를 다시 열려고 시도하는 코드를 추가하면 즉시 깨진다.

---

## 4. 파일 구성

```
webcam-guard\                  # 레포. 배포 위치는 어디든 상관없다
  ├─ guard.py                  # 서버 전체 (약 400줄, 단일 파일)
  ├─ index.html                # 휴대폰 뷰어 (CSS/JS 인라인, 자산만 static/ 참조)
  ├─ .env.example              # 설정 템플릿. 복사해서 .env 로 (그쪽은 gitignore)
  ├─ static\                   # 내장 자산. tools/vendor.py 가 받아둔 것
  │   ├─ hls.min.js            #   hls.js 1.5.13 (Apache-2.0)
  │   ├─ fonts.css             #   @font-face. vendor.py 가 생성 — 손대지 말 것
  │   └─ ibm-plex-*.woff2      #   IBM Plex latin subset (OFL-1.1)
  ├─ README.md                 # 사용자용 설치·운영 안내
  ├─ PROJECT.md                # 이 문서
  ├─ CLAUDE.md                 # 에이전트용 요약 + 절대 규칙 (이 문서가 우선)
  ├─ .gitignore                # 녹화물·비밀번호 든 .bat 커밋 차단
  ├─ .editorconfig             # 4-space / LF, .bat만 CRLF
  ├─ start_guard.bat.example   # 복사 → start_guard.bat (자동 실행용)
  └─ tools\                    # 개발·검증용. 운영에는 배포하지 않아도 된다
      ├─ _common.py            #   DEFAULTS 재사용, 가짜 클립 생성
      ├─ selftest.py           #   카메라 없이 파이프라인 + 전 라우트 검증
      ├─ devserver.py          #   가짜 카메라로 실제 앱 구동 (뷰어 작업용)
      └─ vendor.py             #   static/ 자산 받아오기 (빌드 단계, 1회성)

C:\CamRecordings\  # --root, 실행 시 자동 생성
  ├─ clips\        # 10분짜리 mp4 아카이브
  └─ live\         # HLS 임시 파일 (delete_segments로 계속 순환)
```

`guard.py`와 `index.html`은 **같은 폴더**에 있어야 한다 (`main()`에서 검사 후 없으면 종료).

### guard.py 내부 구성

| 구역 | 역할 |
|---|---|
| `DEFAULTS` | 모든 설정의 단일 출처. argparse가 이 dict를 순회해 CLI 플래그를 자동 생성 |
| `load_dotenv()` / `env_default()` | 설정 우선순위 **CLI 인자 > 환경변수(.env) > DEFAULTS** (5-10절) |
| `STATE` | 스레드 간 공유 상태 (recording, started_at, restarts, last_error) |
| `CLIP_RE` | 파일명 규약 정규식. **파싱·검증·경로차단 3곳에서 재사용** |
| `STATIC_RE` | `/static/` 화이트리스트. 확장자가 js/css/woff2 인 평범한 이름만 통과 |
| `find_font()` / `timestamp_filter()` | 시각 오버레이. 폰트가 없거나 못 쓰면 **필터를 빼고 녹화는 계속한다** |
| `list_devices()` | `--list`용. ffmpeg이 장치 목록을 stderr로 뱉으므로 그대로 출력 |
| `build_input_args()` | 카메라 쪽(dshow) argv만 분리. **도구가 여기만 합성 입력으로 갈아끼운다** |
| `build_command()` | ffmpeg argv 조립. `input_args=`를 주면 입력만 대체되고 출력 옵션은 그대로 |
| `_job_handle()` / `adopt_child()` | ffmpeg을 Windows Job Object에 묶어 고아를 막는다 (5-9절) |
| `supervisor()` | ffmpeg 생명주기 관리 (스레드) |
| `janitor()` | 디스크 정리, 5분 주기 (스레드) |
| `make_handler()` | 클로저로 설정을 캡처해 HTTP 핸들러 클래스 생성 |
| `main()` | 폴더 준비 → 스레드 2개 기동 → HTTP 서버 blocking |

---

## 5. 손대면 안 되는 것 (설계 결정과 이유)

수정 요청이 들어와도 아래는 **이유를 설명하고 대안을 제시**할 것. 무심코 고치면 조용히 망가진다.

### 5-1. tee 타깃은 반드시 상대 경로

```python
subprocess.Popen(cmd, cwd=str(root))   # ← cwd로 root에 들어간 뒤
"[f=segment:...]clips/%Y-%m-%d_%H-%M-%S.mp4|[...]live/live.m3u8"
```

**Why**: tee 먹서는 `:`를 자체 옵션 구분자로 쓴다. 절대 경로 `C:\CamRecordings\...`를 넣으면 드라이브 문자의 콜론이 옵션 구분자로 파싱돼 깨진다. 이스케이프(`C\:\...`)로 해결할 수는 있으나 극도로 취약하다. **cwd를 옮기고 상대 경로를 쓰는 방식이 검증된 해법**이다.

### 5-2. janitor는 가장 최신 파일을 절대 삭제하지 않는다

```python
for p, st in files[:-1]:   # ← [:-1] 의도적
```

**Why**: 목록의 마지막(최신) 파일은 ffmpeg이 지금 이 순간 쓰고 있는 파일이다. 삭제하면 Windows에서 파일 잠금 오류가 나거나 녹화 스트림이 끊긴다.

### 5-3. janitor의 예외는 전부 삼킨다

**Why**: 청소 실패가 녹화를 멈춰선 안 된다. 감시 카메라에서 최우선 순위는 "계속 녹화되는 것"이다. 로그만 남기고 다음 주기를 기다린다.

### 5-4. HLS 출력에 `onfail=ignore`

**Why**: tee는 출력 하나가 실패하면 전체 프로세스를 죽인다. 실시간 보기가 깨지더라도 **아카이브 녹화는 살아 있어야** 하므로 HLS 쪽에만 `onfail=ignore`를 붙였다. 반대로 segment 출력에는 붙이면 안 된다 — 녹화가 조용히 실패하면 감시 카메라의 존재 이유가 사라진다.

### 5-5. 오디오는 `-an`으로 꺼져 있다

**Why**: 대화 녹음은 지역에 따라 법적 문제가 생길 수 있고, 사용자의 요구사항(방 감시)에도 불필요하다. 켜달라는 요청이 오면 이 점을 먼저 알릴 것.

### 5-6. 파일명 규약 `YYYY-MM-DD_HH-MM-SS.mp4`

**Why**: 이 형식이 세 가지를 동시에 만족한다 — ① Windows 금지 문자(`:`) 없음 ② 사전순 정렬 = 시간순 정렬 (janitor가 이 성질에 의존) ③ 뷰어의 24시간 타임라인이 파일명만 파싱해 위치를 계산 (별도 DB·인덱스 불필요).
`CLIP_RE`를 바꾸면 janitor 정렬, `/api/clips` 파싱, `/clips/` 경로 검증이 **동시에** 영향받는다.

### 5-7. `/clips/` 경로 검증은 `CLIP_RE.match(name)`

**Why**: 화이트리스트 방식이다. `Path(p).name`으로 디렉터리 성분을 제거한 뒤, 정규식에 맞는 이름만 통과시킨다. 블랙리스트(`..` 차단)보다 안전하다. 검증 완료(traversal → 404). `/static/`도 `STATIC_RE`로 같은 방식을 쓴다.

### 5-8. drawtext 폰트 경로의 콜론은 백슬래시 **두 개**다

```python
esc = str(font).replace("\\", "/").replace(":", "\\\\:")   # -> C\\:/WINDOWS/Fonts/consola.ttf
```

**Why**: 이 프로젝트에서 실제로 물린 버그다. 원래 코드는 `C\:/...`(백슬래시 1개)였고 **ffmpeg 9.0에서 확실히 실패한다** — 결과는 오버레이만 빠지는 게 아니라 **ffmpeg 전체가 즉시 죽어 녹화가 아예 안 된다**.

이유는 파서가 두 겹이기 때문이다. 필터그래프 파서가 먼저 `:`로 필터를 끊고, 그다음 drawtext가 자기 옵션을 다시 `:`로 끊는다. 백슬래시 1개는 첫 번째 파서가 먹어버려서 두 번째 파서에는 맨 `:`가 도착한다.

2026-08-23에 6가지 표기를 실제로 돌려 확인한 결과:

| 표기 | 결과 |
|---|---|
| `C\:/Windows/...` | **실패** (원래 코드) |
| `C\\:/Windows/...` | 성공 ← **채택** |
| `'C\:/Windows/...'` (작은따옴표) | 성공 |
| `consola.ttf` (상대경로) | 성공 |
| `/Windows/Fonts/...` (드라이브 문자 생략) | 성공하지만 **쓰면 안 된다** — `cwd`가 `--root`라서 `--root D:\...`면 D드라이브에서 폰트를 찾는다 |

같은 이유로 `timestamp_filter()`는 폰트를 못 찾으면 필터를 **통째로 빼고** 녹화를 계속한다. 장식용 오버레이 때문에 영상 전부를 잃는 건 감시 카메라에서 최악의 트레이드다.

### 5-9. ffmpeg은 Job Object에 묶여 있다 — `adopt_child()` 호출을 빼지 말 것

**Why**: `supervisor()`의 `proc.terminate()`는 **정상 종료 경로만** 덮는다. 작업 관리자 종료·`Stop-Process -Force`·정전에는 정리 코드가 아예 실행되지 않아 ffmpeg이 살아남고, 카메라를 계속 점유해서 **그 뒤로는 재시작해도 영영 녹화가 안 된다.** 24시간 운영에서 제일 아픈 실패 모드였다.

Windows는 죽는 프로세스의 핸들을 전부 닫아준다. 그래서 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 잡에 ffmpeg을 넣어두면 **우리가 어떻게 죽든** OS가 자식을 같이 죽인다. `_JOB`을 전역으로 살려두는 것도 이 때문이다 — 핸들이 닫히는 순간이 곧 정리 시점이다.

`ctypes`는 표준 라이브러리라 의존성 0개 원칙에 어긋나지 않는다. 실패하면 로그만 남기고 녹화는 계속한다. `tools/selftest.py`가 실제로 부모를 하드킬해 자식이 죽는지 매번 확인한다.

### 5-10. 설정 우선순위: CLI 인자 > 환경변수(.env) > DEFAULTS

**Why 이 순서인가**: `.env`는 "평소 값"이고 명령줄 인자는 "이번만 다르게"다. 반대로 두면 `--port 9000`을 줘도 `.env`가 이겨서 조용히 무시된다.

구현이 미묘하다. `env_default()`가 argparse의 **default**를 만들고, argparse는 인자가 실제로 주어졌을 때만 그 값을 덮는다. 그래서 "인자가 명시됐는지"를 따로 추적할 필요가 없다. 파싱 후에 `os.environ`을 다시 읽어 덮어쓰는 방식으로 바꾸면 **이 순서가 뒤집히니** 하지 말 것.

두 가지 더:
- **실제 환경변수가 `.env`보다 세다.** `load_dotenv()`는 이미 있는 키를 건드리지 않는다. 일회성으로 `$env:GUARD_PASSWORD`를 준 게 파일에 밀리면 이상하다.
- **`bool` 검사를 `int`보다 먼저 해야 한다.** 파이썬에서 `bool`은 `int`의 하위 클래스라 순서를 바꾸면 `GUARD_TIMESTAMP=false`가 `int("false")`로 가서 깨진다.

`python-dotenv`를 쓰지 않은 이유는 1절의 의존성 0개 원칙 그대로다. `tools/selftest.py`의 `[config]` 블록이 이 우선순위를 통째로 검증한다.

---

## 6. HTTP API 계약

모든 라우트에 HTTP Basic 인증 적용. 뷰어(`index.html`)가 이 스키마에 의존하므로 **바꿀 때는 양쪽을 함께 수정**할 것.

| 메서드 | 경로 | 응답 |
|---|---|---|
| GET | `/` | `index.html` |
| GET | `/api/status` | `{channel, recording, uptime, restarts, last_error, used_gb, max_gb, retain_days, server_time}` |
| GET | `/api/days` | `{days: ["2026-08-23", ...]}` 최신순 |
| GET | `/api/clips?date=YYYY-MM-DD` | `{clips: [{name, date, time, seconds_of_day, size_mb}]}` |
| GET | `/live/live.m3u8`, `/live/segN.ts` | HLS (no-store) |
| GET | `/clips/<name>.mp4` | mp4, **Range 지원**(206) |
| GET | `/static/<name>.{js,css,woff2}` | 내장 자산 (`private, max-age=604800`) |

`seconds_of_day`는 뷰어의 24시간 타임라인이 눈금 위치를 계산하는 값이다 (`seconds_of_day / 86400 * 100%`).

---

## 7. 뷰어(index.html) 디자인 시스템

수정 시 이 토큰 안에서 작업해야 일관성이 유지된다. 임의의 새 색을 추가하지 말 것.

```css
--ink:#0D1117   /* 배경: 푸른 기가 도는 검정 */
--panel:#151B24 /* 패널 */
--line:#25303F  /* 경계선 */
--bone:#E6E1D5  /* 본문 텍스트 (따뜻한 회백색) */
--dim:#78849A   /* 보조 텍스트 */
--sodium:#F2A73B /* 강조색 — 나트륨등(가로등) 호박색 */
```

- **폰트**: IBM Plex Mono(시각·라벨·수치, 대문자+자간), IBM Plex Sans(본문). 계측기·CCTV의 산업적 느낌을 의도한 선택.
- **강조색을 한 곳에만 쓴다**: 녹화 표시등, 선택된 항목, 타임라인 눈금, 용량 게이지. 그 외에는 무채색.
- **시그니처 요소 = 24시간 타임라인 바**(`.strip`). 하루치 녹화를 가로 막대에 눈금으로 뿌리고, 탭하면 그 시각 클립이 재생된다. 상시녹화라는 이 프로젝트의 성격을 그대로 드러내는 요소이므로 **함부로 없애지 말 것**.
- 모바일 우선. `prefers-reduced-motion` 존중, 키보드 포커스 링 있음.
- hls.js는 cdnjs CDN에서 로드. iOS Safari는 네이티브 HLS로 CDN 없이도 재생된다.

---

## 8. 알려진 제약과 함정

| 항목 | 내용 |
|---|---|
| 실시간 지연 | HLS 특성상 **3~6초**. 줄이려면 WebRTC가 필요하고 구조가 크게 복잡해진다 |
| 통신 암호화 | **없음(평문 HTTP)**. 그래서 README가 포트포워딩 대신 Tailscale을 강하게 권한다 |
| 카메라 점유 충돌 | 줌·팀즈 등이 웹캠을 쓰면 ffmpeg이 장치를 못 연다. 증상 = `restarts` 카운터가 계속 증가 |
| ~~고아 ffmpeg~~ **해결됨** | 예전엔 강제 종료 시 ffmpeg이 살아남아 카메라를 점유했다. 지금은 Windows Job Object(`KILL_ON_JOB_CLOSE`)에 묶여 있어 guard.py가 어떻게 죽든 OS가 정리한다. 5-9절 참고. selftest가 실제 하드킬로 매번 검증한다 |
| 비밀번호 노출 | `--password`로 주면 명령줄에 남아 같은 PC의 다른 프로세스에서 보인다. `GUARD_PASSWORD` 환경변수를 쓰면 그 세션 안에만 남는다 |
| 자산 미배포 | `static/` 없이 배포하면 뷰어가 CDN 폴백으로 동작한다. 인터넷이 없으면 안드로이드 실시간 재생이 안 된다 |
| 절전 모드 | PC가 잠들면 녹화가 멈춘다. 전원 옵션에서 절전 해제 필수 |
| Windows 서비스 등록 | **하지 말 것**. 세션 0에서는 웹캠 접근이 안 된다. 시작프로그램(.bat) 방식이 맞다 |
| 디스크 | 720p/15fps/1500k 기준 **하루 약 16GB**. `--max-gb`를 디스크 여유보다 작게 잡지 않으면 C드라이브가 차서 Windows가 멈춘다 |
| 동시 시청 | ThreadingHTTPServer라 몇 명은 되지만, 부하 테스트는 안 했다 |

---

## 9. 다음에 할 만한 작업 (우선순위 제안)

1. **장시간 안정성 관찰** — 2절의 미검증 1번. 하루 이상 켜두고 janitor 삭제와 워치독 재시작을 실제로 보는 것.
2. **모션 감지 녹화 표시** — 상시녹화는 유지하되, 움직임이 있던 시간대를 타임라인에 다른 색으로 표시. ffmpeg `select`/`freezedetect` 또는 별도 경량 분석. 카메라 재점유 불가 제약 때문에 **HLS 세그먼트를 읽어 분석**하는 방식이 현실적이다
3. **이벤트 알림** — 움직임 감지 시 텔레그램/이메일 푸시
4. **HTTPS** — 자체 서명 인증서 또는 Tailscale Serve(무료 TLS 종단) 활용
5. **클립 다운로드/공유 버튼** — 뷰어에 추가
6. **다중 카메라** — 설정을 리스트화하고 채널별 폴더 분리. 구조 변경이 크므로 필요할 때만

---

## 10. 다음 세션 작업 지침

- **사용자에게 실행 결과 원문(오류 메시지 전체)을 요청할 것.** 추측으로 dshow 문제를 고치려 하지 말 것 — 원인이 장치명, 해상도 미지원, 점유 충돌 중 무엇인지는 메시지로만 구분된다.
- **답변에는 항상 "왜"를 포함**하고, 필요한 정보는 사용자에게 되물을 것 (사용자의 명시적 선호).
- 코드를 수정했으면 **반드시** 아래 두 줄을 돌릴 것. 예전처럼 curl을 손으로 치지 말 것 — 도구가 그걸 다 한다.
  ```bash
  python -m py_compile guard.py
  python tools/selftest.py
  ```
  `selftest.py`가 ffmpeg 명령을 `lavfi testsrc` 입력으로 실제 실행해서 tee 두 출력·파일명 규약·HLS 플레이리스트를 확인하고, 이어서 인증·JSON 스키마·Range·경로 탈출까지 본다. **ffmpeg 명령을 바꿨다면 이걸 돌리는 게 검증의 전부다.**
- 뷰어를 건드렸으면 `python tools/devserver.py` — 가짜 카메라(`lavfi`)를 실제 파이프라인에 물려 브라우저로 확인한다. 웹캠이 없어도, 다른 앱이 카메라를 점유 중이어도 된다.
- **도구는 `build_input_args()`만 갈아끼운다.** 출력 옵션을 mock 하지 않는 게 핵심이다 — 실제로 깨지는 건 항상 출력 쪽(tee 이스케이프, 파일명, HLS 플래그)이므로 그 부분은 반드시 진짜 코드여야 한다.
- 기능을 추가할 때는 **의존성 0개 원칙**을 지킬 것. pip 패키지를 넣는 순간 사용자의 설치 난이도가 크게 올라간다. (`tools/`는 개발 전용이라 이 원칙 밖이지만, 거기서도 표준 라이브러리만 썼다.)
