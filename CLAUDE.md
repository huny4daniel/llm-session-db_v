# llm-session-db

여러 PC에서 사용한 Claude Code / Codex 세션을 집 PC 서버에 모아 저장·정리하고, 웹에서 열람·검색하며, 원하는 위치에서 세션을 가져와 대화를 이어갈 수 있게 하는 개인용 세션 서버.

## 배경과 목표

- Claude Code는 세션을 **실행한 폴더 경로에 종속**해 저장한다(`~/.claude/projects/<경로 slug>/<sessionId>.jsonl`). 다른 폴더·다른 PC에서는 해당 세션을 resume할 수 없다.
- 이 프로젝트는 세션을 서버에 보관해 위치 종속을 끊는다. 어느 PC·폴더에서든 서버의 세션을 가져와 이어갈 수 있게 한다.
- 사용자는 1명. 멀티유저·권한 분리는 범위 밖.

## 전체 구성

| 구성요소 | 위치 | 역할 |
|---|---|---|
| 서버 | 집 PC (Windows, 현재 개발 PC) — 배포 exe `LlmSessionServer`로 실행 | 세션 수신·저장, 검색 인덱스, 통계, 웹 UI, 웹 이어가기(B) 실행 |
| 수집 에이전트 | 모든 클라이언트 PC (서버 PC 포함) | 세션 파일 증분 업로드, 세션 가져오기·resume 실행(A) |
| 웹 UI | 서버가 직접 제공 | 세션 목록·대화 뷰·검색·통계·웹 채팅 |
| 관리 GUI | 모든 PC (`python -m agent`/`server` 또는 exe를 명령 없이 실행) | tkinter 창. 에이전트 설정·상태·자동 실행·가져오기 탭, 서버 패키지가 있는 PC에서는 서버 시작·중지·PC 등록·비밀번호 탭 추가. CLI 명령은 그대로 남겨 자동 실행(`run`)·스크립트에 쓴다 |

- 서버 PC의 세션도 원격 PC와 **같은 에이전트 경로**로 수집한다(수집 로직 단일화).
- **배포와 개발을 분리한다**(2026-09-16 사용자 결정): 실제 동작은 릴리즈 exe(서버 PC: `LlmSessionServer`, 다른 PC: `LlmSessionAgent`)로만 하고, 프로젝트 폴더·venv는 개발용이다. 실행 중인 서비스가 프로젝트 폴더에 의존하면 안 된다.
- 원격 접속은 Tailscale 사설망 + 토큰 인증. 서버를 공인 인터넷에 직접 노출하지 않는다.

## 기술 스택

- Python 3.13
- 서버: FastAPI, SQLite(FTS5 전문 검색), SSE(웹 이어가기 스트리밍 — 추가 패키지 없이 재연결·이어받기 지원)
- 에이전트: Python 스크립트(Windows 백그라운드 실행), 관리 GUI는 tkinter(표준 라이브러리, exe에 추가 의존성 없음)
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
- A: 가져온 사본은 수집하지 않는 가져오기 폴더에 두고 `--fork-session`으로 이어간다. CLI가 ID로 사본을 찾고, 새 기록은 작업 폴더 기준 새 파일로 생겨 수집된다(적용됨).
- B: 부분 응답은 UI에만 표시하고 중단 시 유실됨을 안내한다. 도구 승인은 `permission_denials`를 보여주고 허용 도구를 붙여 재실행하는 방식이 가장 단순하다(인라인 승인은 `--permission-prompt-tool` 필요).
- 수집: 같은 ID 파일이 여러 폴더에 있으면 한 세션(machine·source·session_uid)으로 합쳐지므로 세션 내 메시지 `uuid` 기준으로 중복을 거르고, 대화 뷰는 시간순으로 정렬한다(적용됨).

### A. 로컬로 가져와서 이어가기
구현: `agent/pull.py`, `python -m agent pull <세션 번호|세션 ID 앞부분> [--dir 폴더] [--run]`, GUI "가져와 이어가기" 탭(목록은 `GET /api/agent/sessions`, 실행은 새 콘솔 창), 서버 `GET /api/agent/sessions/{ref}/source`(PC 토큰 인증), 웹 세션 화면의 가져오기 명령 복사.

1. 에이전트가 서버에서 세션 정보와 메인 파일 원본 줄을 받는다.
2. 이 PC에 원본 세션 파일이 있으면(원래 PC) 가져오지 않고 `claude --resume <uid>`로 그대로 이어간다.
3. 없으면 `~/.claude/projects/llm-session-db-import/<uid>.jsonl`에 사본을 두고 `claude --resume <uid> --fork-session`으로 이어간다. 사본 폴더는 수집하지 않고, 새 기록은 작업 폴더 기준 새 세션 파일로 생겨 이 PC 에이전트가 수집한다.
4. 작업 폴더: `--dir` > 이 PC에 있는 원래 프로젝트 폴더 > 현재 폴더. 원래 폴더와 다르면 안내한다.
5. `--run`이 없으면 실행할 명령만 보여주고(사본 유지), 있으면 바로 실행한 뒤 사본을 지운다.

- 계획했던 PC별 경로 대응표·`cwd` 치환은 실험 결과(ID로 어느 폴더든 찾음, 치환 불필요) 필요 없어 만들지 않았다.
- 대화 기억만 이동하며 코드 파일은 이동하지 않는다. 같은 저장소가 있는 위치에서 이어가는 것을 전제로 한다.
- 체크포인트(file-history) 이동은 하지 않는다(후순위).

### B. 웹에서 바로 이어가기
구현: `server/runner.py`(실행 관리), `app.py`의 `/api/sessions/{id}/runs`·`/api/runs/*`, 웹 UI 세션 화면 하단 "이어서 대화하기".

- 실행 명령: `claude -p --resume <uid> --output-format stream-json --verbose --include-partial-messages --permission-mode default`. 프롬프트는 표준 입력으로 넘긴다(한국어·긴 입력 안전).
- **이어갈 대상**
  - 서버 PC `~/.claude/projects/*/<uid>.jsonl`이 있으면 그 세션에 그대로 이어간다.
  - 없으면(다른 PC 세션) 수집된 메인 파일 원본으로 `~/.claude/projects/llm-session-db-import/<uid>.jsonl` 사본을 만들고 `--fork-session`으로 새 세션을 만든다. 사본은 실행이 끝나면 지운다. 에이전트는 이 폴더를 수집하지 않는다(`agent/collector.py`의 `IMPORT_FOLDER`).
  - fork로 생긴 새 세션은 `session_links`에 부모를 기록한다(새 세션이 수집되기 전이어도 uid로 연결).
- **실행 위치·도구**
  - 세션의 프로젝트 폴더가 서버 PC에 있으면 그 폴더에서 실행한다. 읽기 도구는 기본 허용, 웹에서 고른 묶음(파일 수정 `edit` / 명령 실행 `shell` / 웹 `web`)만 `--allowedTools`로 추가 허용.
  - 폴더가 없으면 `data/workspaces/<uid>`에서 `--tools ""`(도구 없음)로 대화만 한다.
  - 거부된 도구는 `result.permission_denials`로 보여주고 "허용하고 계속" 시 해당 묶음을 켜고 이어서 요청한다.
- **프로세스**
  - 세션당 1개, 전체 동시 2개. 중단은 프로세스 트리 강제 종료(스트리밍 중이던 응답은 저장되지 않음).
  - 서버가 Claude Code 세션 안에서 시작되면 물려받는 `CLAUDECODE`·`CLAUDE_CODE_SESSION_ID`·메시징 소켓 등의 변수를 자식 CLI에 넘기지 않는다.
  - Windows에서 창이 뜨지 않게 `CREATE_NO_WINDOW`로 실행한다. `claude` 경로는 PATH에서 찾고 `LSDB_CLAUDE_BIN`으로 지정할 수 있다.
- **이벤트**: stream-json을 `init`·`message_start`·`block_start`·`delta`·`assistant`·`user`·`result`·`run_status`로 줄여 메모리에 보관(완료 후 30분). SSE는 `Last-Event-ID`로 끊긴 지점부터 다시 보낸다. 서버를 재시작하면 실행 기록은 사라진다.
- 실행 결과는 CLI가 세션 파일에 쓰고 에이전트가 수집해 기록에 반영된다(웹은 수집될 때까지 확인해 새로고침·새 세션 링크를 띄움).
- 서버 PC에서 명령 실행 권한을 웹에 여는 기능이므로 조회 API와 같은 인증(비밀번호 또는 로컬 접속)을 거친다.

### 분기(fork) 관리
- 같은 세션을 여러 곳에서 이어가면 기록이 갈라진다. 원본 세션을 덮어쓰지 않고 `session_links`(자식 uid → 부모 세션)로 연결한다.
- 연결 경로 두 가지
  - 웹 이어가기: 서버가 실행하며 새 세션 ID를 알므로 바로 기록한다.
  - 그 밖의 fork(`agent pull`, CLI에서 직접 `--fork-session`): 수집 시 새 세션의 첫 메인 묶음에서 다른 세션과 겹치는 메시지 `uuid`를 찾아 부모로 기록한다. 겹침 수가 많은 세션, 같으면 자기만의 메시지가 적은 세션(형제보다 원본)을 고른다.
- 세션을 삭제하면 그 세션이 자식인 링크도 지운다.

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

1. **서버 코어** (v0.0.1): DB 스키마, Claude Code 파서, 서버 PC 세션 수집, 웹 목록·대화 뷰·검색·토큰 통계
2. **원격 접속** (v0.0.2): 웹 UI 비밀번호 로그인, 에이전트·서버 백그라운드 자동 실행(로그 파일·중복 실행 방지), `agent status`, Tailscale 연결 안내(미검증)
   - v0.0.3: 세션 내 메시지 uuid 중복 제거, 세션 삭제(재수집 방지 표시)
3. **웹 이어가기(B)** (v0.0.4): headless CLI 실행 + SSE 스트리밍, 도구 허용 묶음, 중단, fork 부모 연결
4. **로컬 가져오기(A)** (v0.0.5): `agent pull`, 가져오기 사본 + fork, uuid 겹침으로 부모 자동 연결
5. **관리 GUI** (v0.0.6): tkinter 창으로 에이전트 설정·상태·자동 실행·가져오기, 서버 시작·중지·PC 등록·비밀번호. 명령 없이 실행하면 GUI, 기존 CLI 명령 유지. 백그라운드 프로세스는 PID 파일(`agent.pid`, `data/server.pid`)로 중지.
   - v0.0.7: 서버 배포 exe(`LlmSessionServer`, 서버+에이전트+GUI). 배포는 exe, 프로젝트 폴더는 개발용으로 분리.
6. **Codex 지원** (보류, 2026-09-15 사용자 결정): Codex를 실제로 쓰게 되면 진행. 개발 PC에 Codex CLI·세션이 없어 실제 샘플 확보(설치·로그인·대화)부터 시작하고, 파서·에이전트 탐색·이어가기(`codex resume` 동작 실험)를 추가한다. DB `source` 컬럼·`PARSERS` 등록 구조는 준비되어 있다.

## 개발 규칙

- 코드 주석·UI 문구·커밋 메시지는 한국어.
- 커밋 규칙은 전역 CLAUDE.md를 따른다.
- 실제 `~/.claude`, `~/.codex` 파일은 **읽기 전용**으로만 다룬다. 테스트에서 쓰기가 필요하면 임시 디렉터리에 복사해 사용한다. 예외: 웹 이어가기의 가져오기 사본(`llm-session-db-import` 폴더)과 A 방식의 실제 복원 기능.
- 실제 CLI를 실행하는 테스트는 만들지 않는다. `tests/fake_claude.py`(stream-json 흉내)로 대체한다.
- GUI 테스트는 숨긴 `tk.Tk()`에 `App(root)`를 붙여 위젯을 만들고, 스레드 작업 결과는 `agent.gui.dispatch_finished()`로 직접 처리한다(메인 루프 없이 검증). GUI 코드는 얇게 두고 로직은 `status`·`service`·`pull` 모듈에 둔다.
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
| `server/runner.py` | 웹 이어가기: 이어갈 방식 결정, CLI 실행·이벤트 변환·중단, fork 부모 기록 |
| `server/service.py` | 백그라운드 서버 관리(HTTP로 실행 확인, PID 파일로 중지, 자동 실행 등록). CLI `install`/`stop`과 GUI 서버 탭 공유 |
| `server/gui.py` | GUI 서버 탭(`ServerPanel`): 서버 시작·중지·자동 실행, PC 등록·토큰 재발급·이름 변경·삭제(세션 포함, 확인 후), 비밀번호 설정 |
| `agent/` | 수집 에이전트(표준 라이브러리만 사용, 로컬 상태 없이 서버의 파일별 수신 위치 기준으로 증분 전송) |
| `agent/autostart.py` | HKCU Run 키 자동 실행 등록(서버 CLI도 공유). Run 키는 작업 디렉터리를 못 정하므로 `-c`로 프로젝트 경로를 넣어 실행 |
| `agent/lock.py` | 잠금 파일로 에이전트 중복 실행 방지 |
| `agent/service.py` | 백그라운드 에이전트 관리(잠금으로 실행 확인, PID 파일로 중지, 자동 실행 등록). CLI와 GUI 공유 |
| `agent/status.py` | 설정·자동 실행·서버 연결·미전송 파일 보고(`StatusReport`). CLI `status`와 GUI 상태 탭 공유 |
| `agent/gui.py` | tkinter 관리 창(설정·상태·가져오기 탭). 서버 패키지를 불러올 수 있으면 `server.gui.ServerPanel`을 탭으로 붙인다. 오래 걸리는 작업은 스레드 → 큐 → 메인 루프 폴링(`start_dispatcher`)으로 반영 |
| `agent/pull.py` | 서버 세션을 이 PC로 가져와 이어가기. 계획(`plan_pull`) → 사본(`prepare_copy`) → 실행(`launch`, GUI는 새 콘솔 창) 단계로 나눠 CLI·GUI가 공유 |
| `agent/claude_cli.py` | claude 실행 파일 찾기·물려받은 세션 환경 변수 제거(서버 웹 이어가기와 공유) |
| `packaging/agent_entry.py` | 에이전트 exe(PyInstaller) 진입점 |
| `packaging/server_entry.py` | 서버 exe 진입점. 첫 인자가 `agent`면 에이전트 CLI, 아니면 서버 CLI. `autostart.command_prefix`가 서버 exe의 에이전트 자동 실행 명령에 `agent`를 붙인다 |
| `tests/` | pytest. `tests/samples.py`는 실제 구조를 흉내 낸 합성 세션 |

- 파생 테이블 구조나 파서를 바꾸면 `python -m server rebuild`로 원본에서 다시 만든다.
- FastAPI 요청 처리 중 의존성·엔드포인트가 다른 스레드에서 돌 수 있어 DB 연결은 요청마다 새로 만들고 `check_same_thread=False`로 연다.

## 실행 방법

```powershell
# 최초 1회
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt

# 서버 (기본 127.0.0.1:8765, DB·server.log는 data/ — LSDB_DATA_DIR로 변경)
.venv\Scripts\python -m server                        # 관리 GUI(서버 탭: 시작·중지·자동 실행·PC 등록·비밀번호)
.venv\Scripts\python -m server add-machine home-pc     # PC별 토큰 발급(한 번만 표시)
.venv\Scripts\python -m server set-password            # 원격 접속 시 필수(설정하면 로컬도 로그인 필요)
.venv\Scripts\python -m server serve                   # 포그라운드 실행
.venv\Scripts\python -m server install                 # 로그인 시 자동 실행 등록 + 지금 백그라운드 시작
.venv\Scripts\python -m server uninstall

# 에이전트 (설정·agent.log·agent.lock·agent.pid: ~/.llm-session-db/ — LSDB_AGENT_CONFIG로 변경)
python -m agent                 # 관리 GUI(설정·상태·가져오기 탭). exe는 더블클릭
python -m agent setup --server http://<서버>:8765 --token <토큰>
python -m agent run             # 포그라운드 30초 간격, --once는 한 번만
python -m agent status          # 설정·자동 실행·서버 연결·미전송 파일
python -m agent pull <번호|ID>  # 서버 세션을 이 PC로 가져와 이어가기(--dir 폴더, --run 바로 실행)
python -m agent install         # 로그인 시 자동 실행(pythonw, 창 없음) + 지금 시작
python -m agent uninstall
python -m agent stop            # 백그라운드 에이전트 종료(PID 파일 기준)

# 테스트
.venv\Scripts\python -m pytest
```

- 위는 개발용 실행이다. 실제 설치는 GitHub Release의 exe로 한다.
  - 서버 PC: `LlmSessionServer_vX.Y.Z.exe`를 고정 폴더에 두고 GUI 서버 탭에서 자동 실행 등록. 에이전트 명령은 `LlmSessionServer_vX.Y.Z.exe agent <명령>`. 데이터는 기본 exe 옆 `data\`(`LSDB_DATA_DIR`로 변경).
  - 다른 PC: `LlmSessionAgent_vX.Y.Z.exe`(에이전트+GUI). 명령은 같다(`setup ...`, `status`, `install`).
  - exe로 `install`하면 exe 자신의 경로가 자동 실행에 등록되므로 exe를 옮기지 않을 위치에 두고, 버전을 올릴 때는 멈추고 새 exe로 다시 `install`한다.
- 에이전트는 표준 라이브러리만 쓰므로 Python이 있는 원격 PC는 저장소의 `agent/` 폴더만으로도 동작한다.

## 릴리즈

- `v*` 태그 푸시 → `.github/workflows/release.yml`이 에이전트 exe와 서버 exe를 빌드해 릴리즈에 첨부하고, 같은 major.minor의 기존 릴리즈를 삭제한다.
- 진입점은 `packaging/agent_entry.py`(에이전트)와 `packaging/server_entry.py`(서버+에이전트). GUI와 CLI를 한 exe로 제공하므로 전역 표준의 `--windowed` 대신 `--console --hide-console hide-early`로 빌드한다(터미널 실행 시 출력 유지, 더블클릭·로그인 자동 실행처럼 콘솔이 새로 생길 때만 창을 숨겨 GUI만 보임). GUI에서 claude 실행은 `CREATE_NEW_CONSOLE`로 새 터미널을 띄운다.
- 서버 exe는 `--add-data "server/static;server/static"`으로 웹 UI를 넣고 `--collect-submodules uvicorn`으로 uvicorn이 문자열로 불러오는 모듈을 포함한다. 배포 exe의 기본 데이터 폴더는 exe 옆 `data\`(`server/config.default_data_dir`).
- 로컬 빌드 확인: `.venv\Scripts\pip install pyinstaller` 후 위 워크플로와 같은 옵션으로 `pyinstaller` 실행(`build/`, `dist/`, `*.spec`은 gitignore).
- onefile exe가 자기 자신을 백그라운드로 다시 실행할 때(`install` → `serve`/`run`)는 `_PYI_*`·`_MEIPASS2` 환경 변수를 빼고 띄운다(`autostart.detached_env`). 안 빼면 자식이 부모의 임시 압축 해제 폴더를 같이 쓰다가 부모가 끝나며 폴더를 지워 자식이 조용히 죽는다(v0.0.7에서 겪음). 로컬 빌드 후 `exe install --port <임시 포트>`로 백그라운드 기동까지 확인한다.
- `uninstall`은 등록만 해제한다. 실행 중인 프로세스는 `stop`(또는 GUI 중지)으로 종료한다. PID 파일이 없는 이전 버전 프로세스는 작업 관리자에서 직접 종료한다.

## 원격 접속 (Tailscale)

> 개발 PC에 Tailscale이 설치되어 있지 않아 아래 절차는 아직 실제로 검증하지 않았다.

1. 서버 PC와 각 클라이언트 PC에 Tailscale을 설치하고 같은 계정으로 로그인한다.
2. 서버 PC: `python -m server set-password`
3. 서버 공개 방법(둘 중 하나)
   - **권장: `tailscale serve`** — 서버는 기본값(127.0.0.1)으로 두고 `tailscale serve --bg 8765`로 tailnet에만 HTTPS로 노출한다. 바인딩 순서 문제나 방화벽 설정이 없고, HTTPS라 브라우저 클립보드 API도 동작한다. 프록시 경유 요청은 원격으로 판별되어 로그인이 요구된다.
   - 직접 바인딩 — `python -m server install --host <서버의 Tailscale IP 100.x.y.z>`. 부팅 직후 Tailscale보다 먼저 뜨면 바인딩에 실패할 수 있고, Windows 방화벽 허용이 필요하다. `0.0.0.0`은 같은 LAN에도 열리므로 피한다.
4. 서버 PC: `python -m server add-machine <클라이언트 이름>`으로 토큰 발급
5. 클라이언트 PC: `python -m agent setup --server <3에서 정한 주소> --token <토큰>` → `python -m agent status`로 연결 확인 → `python -m agent install`
