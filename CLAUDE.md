# llm-session-db

여러 PC에서 사용한 Claude Code / Codex 세션을 집 PC 서버에 모아 저장·정리하고, 웹에서 열람·검색하며, 원하는 위치에서 세션을 가져와 대화를 이어갈 수 있게 하는 개인용 세션 서버.

## 배경과 목표

- Claude Code는 세션을 **실행한 폴더 경로에 종속**해 저장한다(`~/.claude/projects/<경로 slug>/<sessionId>.jsonl`). 다른 폴더·다른 PC에서는 해당 세션을 resume할 수 없다.
- 이 프로젝트는 세션을 서버에 보관해 위치 종속을 끊는다. 어느 PC·폴더에서든 서버의 세션을 가져와 이어갈 수 있게 한다.
- 사용자는 1명. 멀티유저·권한 분리는 범위 밖.

## 전체 구성

| 구성요소 | 위치 | 역할 |
|---|---|---|
| 서버 | 집 PC (Windows, 현재 개발 PC) | 세션 수신·저장, 검색 인덱스, 통계, 웹 UI, 웹 이어가기(B) 실행 |
| 수집 에이전트 | 모든 클라이언트 PC (서버 PC 포함) | 세션 파일 증분 업로드, 세션 가져오기·resume 실행(A) |
| 웹 UI | 서버가 직접 제공 | 세션 목록·대화 뷰·검색·통계·웹 채팅 |

- 서버 PC의 세션도 원격 PC와 **같은 에이전트 경로**로 수집한다(수집 로직 단일화).
- 원격 접속은 Tailscale 사설망 + 토큰 인증. 서버를 공인 인터넷에 직접 노출하지 않는다.

## 기술 스택

- Python 3.13
- 서버: FastAPI, SQLite(FTS5 전문 검색), WebSocket(웹 이어가기 스트리밍)
- 에이전트: Python 스크립트(Windows 백그라운드 실행)
- 웹 UI: 서버에서 정적 파일로 제공(별도 프론트엔드 빌드 없이 시작)

## 데이터 소스

### Claude Code
- 경로: `~/.claude/projects/<slug>/<sessionId>.jsonl`
- 서브에이전트: `~/.claude/projects/<slug>/<sessionId>/subagents/agent-<agentId>.jsonl` (줄의 `sessionId`는 부모 세션, `agentId`·`isSidechain: true`, 첫 줄 `fork-context-ref`에는 `sessionId` 없음 → 세션 식별은 파일 경로 기준)
- 같은 폴더의 `tool-results/*.txt`, `subagents/*.json`, 프로젝트 폴더의 `memory/`는 세션 JSONL이 아니므로 수집하지 않는다.
- slug 규칙: cwd 절대경로에서 영숫자가 아닌 문자(한글 포함)를 모두 `-`로 치환
  - 예: `C:\Users\USER\Desktop\Project\llm-session-db_v` → `C--Users-USER-Desktop-Project-llm-session-db-v`
- 한 줄 = 이벤트 1개. `type`으로 구분(`user`, `assistant`, `file-history-snapshot`, `mode`, `permission-mode` 등)
- 공통 필드: `sessionId`, `uuid`, `parentUuid`, `timestamp`, `cwd`, `gitBranch`, `version`, `isSidechain`
- `assistant` 이벤트의 `message.model`, `message.usage`(input/output/cache_creation/cache_read/thinking 토큰)로 사용량 집계
  - 한 API 응답이 콘텐츠 블록마다 별도 줄로 기록되고 줄마다 누적 `usage`가 반복된다 → `message.id` 단위로 최댓값만 센다.
  - `model: "<synthetic>"`은 CLI가 만든 오류 메시지라 사용량에서 제외한다.
- 세션 메타: `ai-title`(제목, 마지막 값), `last-prompt`, `cost-state`(누적 `totalCostUSD`, 마지막 값)
- `user` 이벤트 분류: `isMeta` → meta, `isCompactSummary` → 압축 요약, 전부 `tool_result` 블록 → 도구 결과, `[Request interrupted…` → 중단, `<command-name>`·`<task-notification>` 등 CLI 태그 → command, 그 외가 실제 사용자 질문
- 파일 체크포인트 백업은 `~/.claude/file-history`에 별도 저장

### Codex CLI
- 세션 메타: `~/.codex/state_5.sqlite`의 `threads` 테이블(`id`, `rollout_path`, `cwd`, `title`, `model_provider`, `created_at`, `updated_at`)
- 대화 본문: `rollout_path`가 가리키는 JSONL(`~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`)
- 현재 개발 PC에는 Codex 세션 데이터가 없으므로 실제 샘플 확보 후 파서를 작성한다.

> 두 도구 모두 세션 파일 형식이 공식 문서화되어 있지 않아 버전 업데이트로 바뀔 수 있다.

## 핵심 설계 원칙

1. **원본 보존**: 모든 이벤트의 원본 JSON 줄을 그대로 저장한다. 정규화 테이블은 원본에서 언제든 재생성 가능해야 한다(파서 수정 후 재파싱).
2. **소스별 파서 분리**: `claude`, `codex` 파서를 독립 모듈로 두고 공통 모델로 변환한다. 알 수 없는 이벤트 타입은 버리지 말고 원본으로만 보관.
3. **증분 수집**: 파일별 byte offset + mtime + 크기를 기록해 추가된 줄만 전송. 진행 중 세션의 마지막 불완전한 줄(개행 없음)은 다음 수집으로 미룬다.
4. **멱등 업로드**: 원본 줄은 (machine_id, source, file_key, byte_offset)로 한 번만 저장하고, 메시지는 세션 내 `uuid`로 한 번만 만든다.
6. **삭제는 표시로 막는다**: 세션 삭제 시 원본 줄·파생 데이터를 지우고 `deleted_sessions`에 남긴다. 수신 위치는 유지해 에이전트가 되살리지 않게 하고, 이후 들어오는 줄은 저장 없이 위치만 옮긴다.
5. **대기열 없는 재전송**: 에이전트는 서버에 기록된 파일별 수신 위치(`source_files.next_offset`)부터 보낸다. 서버가 꺼져 있어도 원본 JSONL 자체가 대기열 역할을 하므로 별도 로컬 대기열을 두지 않는다(예외: 오프라인인 동안 CLI가 오래된 세션 파일을 정리하면 그 부분은 유실).

## 세션 이어가기

### 검증된 CLI 동작 (Claude Code 2.1.272, 2026-09-15 실험)

resume
- `claude --resume <ID>`(대화형·`-p` 모두)는 현재 폴더와 무관하게 `~/.claude/projects/*/<ID>.jsonl`을 **ID로 찾는다**. 폴더 이름이 실제 경로와 달라도(존재하지 않는 경로 slug) 찾는다.
- 새 대화는 **찾은 파일에 그대로 이어 쓴다**. 새 줄의 `cwd`만 실제 실행 폴더로 기록된다. 도구(Bash 등)도 실행 폴더 기준으로 동작한다.
- 같은 ID 파일이 여러 폴더에 있으면 **현재 폴더 slug의 파일**에 이어 쓴다.
- 파일 안의 `cwd`를 치환하지 않아도 이어가기가 된다.

fork (`--resume <ID> --fork-session`)
- 새 세션 ID로 **현재 폴더 slug에 새 파일**을 만들고 기존 기록 전체를 복사한다(모든 `sessionId`가 새 ID, `cwd`도 현재 폴더로 바뀜). 원본 파일은 변경되지 않는다.
- fork 파일에는 원본 세션 ID가 남지 않는다. 복사된 메시지의 `uuid` 일부가 원본과 같으므로 **uuid 겹침으로 부모 세션을 추정**해야 한다.

headless (`-p`)
- `-p` 실행도 resume 가능한 세션으로 저장된다(`--no-session-persistence`로 끌 수 있음). 사용자 전역 CLAUDE.md·설정이 적용된다.
- `--output-format stream-json`은 `--verbose`가 필수(없으면 오류 종료).
- 이벤트: `system/init`(session_id·cwd·model·tools·permissionMode 등) → `assistant`(콘텐츠 블록 단위) / `user`(tool_result) → `result`(result·session_id·total_cost_usd·usage·permission_denials). resume해도 session_id는 그대로. 그 외 `system/thinking_tokens`, `system/status`, `rate_limit_event`.
- `--include-partial-messages`를 붙이면 `stream_event`(message_start, content_block_start/delta/stop, message_delta/stop)로 글자 조각이 온다.
- 승인이 필요한 도구는 **대기하지 않고 즉시 거부**된다: `system/permission_denied` 이벤트, `tool_result`(is_error), `result.permission_denials`(tool_name·tool_use_id·tool_input)가 남고 프로세스는 정상 종료. 사용자 설정이 auto 모드여도 `-p`의 init은 default였다. `--allowedTools`로 허용하면 실행된다.
- 응답 스트리밍 도중 프로세스를 강제 종료하면 **스트리밍된 부분 응답은 세션 파일에 저장되지 않는다**(사용자 메시지만 남음). 파일은 깨지지 않고 이후 resume도 정상.

설계 반영
- A: 가져온 세션은 현재 폴더 slug에 두면 된다(ID 검색·중복 시 우선순위 모두 충족). 이어간 기록이 원본 파일에 쌓이는 문제를 피하려면 `--fork-session`을 기본으로 한다.
- B: 부분 응답은 UI에만 표시하고 중단 시 유실됨을 안내한다. 도구 승인은 `permission_denials`를 보여주고 허용 도구를 붙여 재실행하는 방식이 가장 단순하다(인라인 승인은 `--permission-prompt-tool` 필요).
- 수집: 같은 ID 파일이 여러 폴더에 있으면 한 세션(machine·source·session_uid)으로 합쳐지므로 세션 내 메시지 `uuid` 기준으로 중복을 거르고, 대화 뷰는 시간순으로 정렬한다(적용됨).

### A. 로컬로 가져와서 이어가기
1. 에이전트가 서버에서 세션 원본을 내려받는다.
2. 각 이벤트의 `cwd`를 현재 위치로 치환하고, 현재 cwd의 slug 폴더에 저장한다.
3. `claude --resume <sessionId>` 실행(기본은 `--fork-session`으로 새 세션 ID 발급).
4. 이어간 세션은 다시 수집되어 원본 세션의 자식(분기)으로 연결된다.

- PC별 프로젝트 경로 대응표(machine_id + 프로젝트 → 로컬 경로)를 서버에서 관리한다.
- 대화 기억만 이동하며 코드 파일은 이동하지 않는다. 같은 저장소가 있는 위치에서 이어가는 것을 전제로 한다.
- 체크포인트(file-history) 이동은 후순위 확장 기능.

### B. 웹에서 바로 이어가기
- 서버 PC에서 `claude -p --resume <sessionId> --output-format stream-json` 형태로 CLI를 headless 실행하고 WebSocket으로 스트리밍한다.
- 서버 PC에 해당 프로젝트 경로가 있으면 그 폴더에서 실행, 없으면 대화 전용 모드로 표시한다.
- 서버 PC에서 명령 실행 권한을 웹에 여는 기능이므로 인증 없이 절대 동작하지 않게 한다.

### 분기(fork) 관리
- 같은 세션을 여러 곳에서 이어가면 기록이 갈라진다. 원본 세션을 덮어쓰지 않고 부모-자식 관계(`parent_session_id`)로 저장한다.

## 보안

- 세션에는 API 키·토큰·`.env` 내용·소스 코드가 포함될 수 있다.
- 서버를 공인 인터넷에 직접 노출하지 않는다. 원격 접속은 Tailscale 사설망으로만 한다.
- 에이전트 ↔ 서버 통신은 PC별 발급 토큰(DB에는 SHA-256 해시만 저장)으로 인증한다.
- 웹 UI 조회 API(`server/auth.py`)
  - 비밀번호 미설정: 루프백에서 온 요청만 허용. 프록시 헤더(`X-Forwarded-For`, `Tailscale-User-Login` 등)가 있으면 원격으로 간주(`tailscale serve` 경유 요청이 루프백으로 보이는 문제 방지).
  - 비밀번호 설정: 로컬 포함 모든 조회 요청에 로그인 쿠키 필요. PBKDF2-SHA256 해시, HMAC 서명 쿠키(HttpOnly, SameSite=Strict, 30일), 비밀번호 변경 시 서명 키 교체로 전체 로그아웃, 연속 실패 시 일시 잠금.
  - `serve`/`install`은 비밀번호 없이 루프백 외 주소 바인딩을 거부한다.
- 이후 상태를 바꾸는 API(웹 이어가기 등)를 추가할 때도 SameSite=Strict 쿠키에 의존하므로 GET으로 부작용을 만들지 않는다.
- 선택 기능: 저장 시 민감값 패턴 마스킹(원본 보존 원칙과 충돌하므로 설정으로 선택).
- 토큰·DB 파일·로컬 설정은 저장소에 커밋하지 않는다.

## 개발 단계

1. **서버 코어** (v0.0.1 완료): DB 스키마, Claude Code 파서, 서버 PC 세션 수집, 웹 목록·대화 뷰·검색·토큰 통계
   - 미완: 다른 cwd로 옮긴 세션 파일이 `--resume <ID>`로 정상 동작하는지 검증 (4단계 전에 실행)
2. **원격 접속**: 웹 UI 비밀번호 로그인, 에이전트·서버 백그라운드 자동 실행(로그 파일·중복 실행 방지), `agent status`, Tailscale 연결 안내
3. **웹 이어가기(B)**: headless CLI 실행 + WebSocket 스트리밍
4. **로컬 가져오기(A)**: 경로 대응표, cwd 치환, fork 연결
5. **Codex 지원**: 실제 샘플 기반 파서 작성

## 개발 규칙

- 코드 주석·UI 문구·커밋 메시지는 한국어.
- 커밋 규칙은 전역 CLAUDE.md를 따른다.
- 실제 `~/.claude`, `~/.codex` 파일은 **읽기 전용**으로만 다룬다. 테스트에서 쓰기가 필요하면 임시 디렉터리에 복사해 사용한다(A 방식의 실제 복원 기능 제외).
- Codex의 SQLite는 CLI가 사용 중일 수 있으므로 복사본 또는 읽기 전용 연결로 접근한다.
- 파서 테스트용 샘플은 민감정보를 제거한 축약본만 저장소에 둔다.

## 코드 구조

| 경로 | 역할 |
|---|---|
| `server/db.py` | SQLite 스키마(원본 `raw_events` → 파생 `sessions`/`messages`/`api_usage`, FTS5 trigram 인덱스) |
| `server/parsers/claude.py` | Claude Code 줄 파서(세션 메타·메시지 분류·사용량·대화 뷰 블록) |
| `server/ingest.py` | 증분 수신(offset 검증, 멱등 저장), 세션 통계 갱신, 재생성(`rebuild`) |
| `server/queries.py` | 세션 목록·상세·검색·통계 조회 |
| `server/app.py` | FastAPI 앱(에이전트 API `/api/agent/*`, 조회 API, 정적 UI) |
| `server/static/` | 웹 UI(프레임워크 없는 단일 페이지, 해시 라우팅) |
| `server/auth.py` | 웹 UI 비밀번호·로그인 쿠키·로컬 요청 판별·로그인 시도 제한 |
| `agent/` | 수집 에이전트(표준 라이브러리만 사용, 로컬 상태 없이 서버의 파일별 수신 위치 기준으로 증분 전송) |
| `agent/autostart.py` | HKCU Run 키 자동 실행 등록(서버 CLI도 공유). Run 키는 작업 디렉터리를 못 정하므로 `-c`로 프로젝트 경로를 넣어 실행 |
| `agent/lock.py` | 잠금 파일로 에이전트 중복 실행 방지 |
| `packaging/agent_entry.py` | 에이전트 exe(PyInstaller) 진입점 |
| `tests/` | pytest. `tests/samples.py`는 실제 구조를 흉내 낸 합성 세션 |

- 파생 테이블 구조나 파서를 바꾸면 `python -m server rebuild`로 원본에서 다시 만든다.
- FastAPI 요청 처리 중 의존성·엔드포인트가 다른 스레드에서 돌 수 있어 DB 연결은 요청마다 새로 만들고 `check_same_thread=False`로 연다.

## 실행 방법

```powershell
# 최초 1회
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt

# 서버 (기본 127.0.0.1:8765, DB·server.log는 data/ — LSDB_DATA_DIR로 변경)
.venv\Scripts\python -m server add-machine home-pc     # PC별 토큰 발급(한 번만 표시)
.venv\Scripts\python -m server set-password            # 원격 접속 시 필수(설정하면 로컬도 로그인 필요)
.venv\Scripts\python -m server serve                   # 포그라운드 실행
.venv\Scripts\python -m server install                 # 로그인 시 자동 실행 등록 + 지금 백그라운드 시작
.venv\Scripts\python -m server uninstall

# 에이전트 (설정·agent.log·agent.lock: ~/.llm-session-db/ — LSDB_AGENT_CONFIG로 변경)
python -m agent setup --server http://<서버>:8765 --token <토큰>
python -m agent run             # 포그라운드 30초 간격, --once는 한 번만
python -m agent status          # 설정·자동 실행·서버 연결·미전송 파일
python -m agent install         # 로그인 시 자동 실행(pythonw, 창 없음) + 지금 시작
python -m agent uninstall

# 테스트
.venv\Scripts\python -m pytest
```

- 에이전트는 표준 라이브러리만 쓰므로 원격 PC에는 Python과 저장소의 `agent/` 폴더만 있으면 된다.
- Python이 없는 PC는 GitHub Release의 `LlmSessionAgent_vX.Y.Z.exe`를 쓴다. 명령은 같다(`LlmSessionAgent_vX.Y.Z.exe setup ...`, `status`, `install`). exe로 `install`하면 exe 자신이 자동 실행에 등록되므로 exe를 옮기지 않을 위치에 둔다.

## 릴리즈

- `v*` 태그 푸시 → `.github/workflows/release.yml`이 에이전트 exe를 빌드해 릴리즈에 첨부하고, 같은 major.minor의 기존 릴리즈를 삭제한다.
- 진입점은 `packaging/agent_entry.py`. 에이전트는 CLI라 전역 표준의 `--windowed` 대신 `--console --hide-console hide-early`로 빌드한다(터미널 실행 시 출력 유지, 로그인 자동 실행처럼 콘솔을 직접 띄울 때만 창 숨김).
- 서버는 이 PC에서 저장소 + venv로 실행하므로 exe로 배포하지 않는다.
- 로컬 빌드 확인: `.venv\Scripts\pip install pyinstaller` 후 위 워크플로와 같은 옵션으로 `pyinstaller` 실행(`build/`, `dist/`, `*.spec`은 gitignore).
- `uninstall`은 등록만 해제한다. 이미 실행 중인 프로세스는 직접 종료한다.

## 원격 접속 (Tailscale)

> 개발 PC에 Tailscale이 설치되어 있지 않아 아래 절차는 아직 실제로 검증하지 않았다.

1. 서버 PC와 각 클라이언트 PC에 Tailscale을 설치하고 같은 계정으로 로그인한다.
2. 서버 PC: `python -m server set-password`
3. 서버 공개 방법(둘 중 하나)
   - **권장: `tailscale serve`** — 서버는 기본값(127.0.0.1)으로 두고 `tailscale serve --bg 8765`로 tailnet에만 HTTPS로 노출한다. 바인딩 순서 문제나 방화벽 설정이 없고, HTTPS라 브라우저 클립보드 API도 동작한다. 프록시 경유 요청은 원격으로 판별되어 로그인이 요구된다.
   - 직접 바인딩 — `python -m server install --host <서버의 Tailscale IP 100.x.y.z>`. 부팅 직후 Tailscale보다 먼저 뜨면 바인딩에 실패할 수 있고, Windows 방화벽 허용이 필요하다. `0.0.0.0`은 같은 LAN에도 열리므로 피한다.
4. 서버 PC: `python -m server add-machine <클라이언트 이름>`으로 토큰 발급
5. 클라이언트 PC: `python -m agent setup --server <3에서 정한 주소> --token <토큰>` → `python -m agent status`로 연결 확인 → `python -m agent install`
