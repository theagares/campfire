# securedoc-gateway 개발 계획

> 2026-07-19 확정 | PII 마스킹 + 인젝션 마스킹을 다른 에이전트에 연결해주는 로컬 우선 게이트웨이

---

## 1. 확정된 결정사항

| 항목 | 결정 | 비고 |
|---|---|---|
| 앱 이름 | **securedoc-gateway** | 프로젝트 폴더명 동일 |
| 엔진 위치 | **로컬 우선** | 익스텐션 단독 설치 시 AWS 서버 폴백, 앱 설치 시 로컬 우선. 향후 GPU 모델도 로컬 우선 |
| 에이전트 연결 방식 | **MCP** | Ollama 프록시 형태 대비 판단 근거는 §4 |
| 탐지 방식 | **룰베이스 (v1)** | PII → 인코더 모델, 인젝션 → LLM+MCP로 교체 예정. 모듈화 필수 (§5) |
| 인젝션 기본 정책 | **마스킹 후 통과** | 탐지 내역은 메타데이터로 반환. 차단(fail-closed)은 설정 옵션으로 제공 |
| 파서 | **기존 계획 재사용** | `파이프라인/문서_파서_비교.md`의 포맷별 분기 그대로 |
| 데스크탑 앱 | **대시보드형** | Electron + Python 사이드카 |
| 익스텐션 HITL | **Chrome 네이티브 사이드 패널** | 기존 팝업 모달 → 화면 우측 절반. **평소엔 존재하지 않음(닫힌 상태)** — 마스킹된 문서/프롬프트 결과가 돌아왔을 때만 열림, 기본/유휴 상태 없음 |
| 익스텐션 popup | **있음 — 설정 전용 (2026-07-23 재확정)** | 우측 상단 버튼 클릭 시 뜨는 설정 전용 popup. (2026-07-21엔 Figma 트레이 팝업과 혼동해 "제거"로 정정했었으나, 2026-07-23 사용자가 사이드패널과 별개로 설정 전용 popup을 명시적으로 다시 결정 — 이번엔 혼동이 아니라 확정) |
| 익스텐션 설정 UI 위치 | **우측 상단 버튼 → 설정 popup** | (2026-07-23 확정, §변경2 재작성) 사이드패널 탭 통합안 폐기. 서버 설정(로컬 포트 확인, 현재 연결 대상 표시 — **원격 URL 편집은 여기 없음, 앱 설정에만 있음**), 앱 설정(엔진 연결 상태/대시보드 열기/미설치 안내) |
| 인터셉트 범위 | **파일 + 프롬프트 둘 다** | 프롬프트는 전송 시점 훅 |
| GPU 상주 정책 | **PII 인코더 항시 상주 + 인젝션 LLM 유휴 언로드** | §4.1. 모델 미로드 시 검사 생략 없이 fail-closed 대기 |
| 원본 텍스트 보관 | **세션 중 메모리만, 영속 저장 안 함** | §9.1. 이력/DB엔 마스킹본 + 탐지 위치·유형만 남김 |
| 대시보드 접근 제어 | **없음 (로컬 단일 사용자 가정)** | PIN/인증 레이어 없이 바로 진입 |
| 파싱 실패/미지원 포맷 | **경고 후 통과 허용** | §9.2. "모든 파일 탐지" 원칙의 명시적 예외로 기록 |
| 익스텐션 v1 사이트 범위 | **6개 사이트 동시 지원** | chatgpt/claude/gemini/copilot/grok/perplexity |
| 룰베이스 언어 범위 | **한국어만 (v1)** | 기존에 준비된 룰 재사용, 지금은 간단하게. 영문권 패턴은 범위 밖 |
| 자동 업데이트 | **앱만 (electron-updater)** | 익스텐션은 수동(미배포 가정) |
| HITL 타임아웃 처리 | **경고 후 통과 허용** | 파싱 실패(§9.2)와 동일 정책, 동일하게 `scan_status`로 추적 |

---

## 2. 전체 아키텍처

```
┌─────────────────────┐        ┌──────────────────────────┐
│  Chrome 익스텐션     │        │  외부 에이전트            │
│  ┌ interceptor      │        │  (Claude Code/Desktop,   │
│  │  파일 + 프롬프트   │        │   Cursor 등 MCP 클라이언트)│
│  ┌ 사이드 패널        │        └───────────┬──────────────┘
│  │  (HITL 전용,       │                    │ MCP
│  │   평소엔 닫힘)     │                    │
│  ┌ popup (설정 전용,  │                    │
│    우측상단 버튼)     │                    │
└─────────┬───────────┘                    │
          │ HTTP (localhost 우선, AWS 폴백)  │
          ▼                                ▼
┌──────────────────────────────────────────────────────────┐
│  Python 엔진 (FastAPI, localhost:48200)                   │
│                                                          │
│  adapters/                                               │
│  ├ http_api/    REST: /health /jobs /jobs/prompt /events │
│  ├ mcp/         Streamable HTTP MCP (/mcp) + stdio shim  │
│  └ (future) ollama_proxy/  ← 어댑터 한 겹만 추가하면 됨     │
│                                                          │
│  core/                                                   │
│  ├ parser/      포맷별 분기 (§6)                          │
│  ├ detectors/   Detector 인터페이스 + 레지스트리 (§5)      │
│  │  ├ pii/        encoder(룰베이스 폴백 제거됨)            │
│  │  └ injection/  llm_mcp(룰베이스 폴백 제거됨)           │
│  ├ masker/      위치검증 · 겹침병합 · 뒤에서부터 치환        │
│  └ pipeline/    parse → chunk → detect → mask → wrap     │
│                                                          │
│  store/         SQLite(이력·통계) + 설정 + 감사로그(JSONL) │
└──────────────────────────┬───────────────────────────────┘
                           │ 사이드카 (spawn/관리)
┌──────────────────────────┴───────────────────────────────┐
│  Electron 대시보드 앱                                      │
│  홈 · 연결 · 대시보드(통계 통합) · 처리현황 + 트레이 상주      │
│  + 좌하단 설정 톱니 아이콘(팝업). 이력 제거, 룰 관리 v1 미지원 │
└──────────────────────────────────────────────────────────┘
```

핵심 원칙: **코어는 순수 엔진 모듈, 모든 외부 접점(REST/MCP/향후 Ollama)은 어댑터.**
익스텐션·대시보드·에이전트가 전부 같은 엔진 하나를 공유한다.
(2026-07-23 정정) 이전엔 "대시보드에서 에이전트 MCP 호출 이력까지 한 화면에서
보인다"고 적혀 있었으나, §8에서 "이력" 화면 자체를 v1에서 제거하기로 확정하며
**MCP 호출 이력도 v1 스코프에서 함께 드롭**한다 — 이를 볼 수 있는 화면은 없다.
"연결" 화면(§8)이 보여주는 건 활성 세션 유무(연결됨/미연결)뿐, 호출 로그가 아니다.

---

## 3. 서버 선택 로직 (익스텐션)

```
익스텐션 시작 / 캐시된 포트 요청 실패 시:
  48200~48209 10개 포트에 병렬 GET /health (각 500ms 타임아웃, §11 상세)
    ├ "service": "securedoc-gateway" 시그니처 일치 응답 있음 → 그 포트로 로컬 엔진 사용 (캐싱)
    └ 전부 실패/불일치 → 설정된 원격 URL (기본: https://api.airookieupsecurity.com)
```

- 앱이 깔려 있으면 자동으로 로컬 우선, 없으면 AWS 폴백 — 사용자 개입 불필요.
- **포트는 익스텐션이 자동 탐지만 하고, 사용자가 직접 입력/수정하지 않는다**
  (2026-07-23 확정 — 구체적 탐지 방식은 §11 "포트 충돌" 참고). 설정 popup(우측 상단
  버튼)에는 탐지된 포트와 현재 연결 대상(로컬/원격)을 **읽기 전용으로 표시**만 한다(§변경2).
  **원격 URL 자체는 여기서 수정할 수 없다** — 위 기본값을 그대로 쓰거나, 앱이 설치돼
  있으면 앱의 설정 팝업(§8)에서만 바꾼다(2026-07-23 결정: 원격 URL은 앱 쪽에만 존재).
- 엔진의 REST API는 기존 EC2 서버와 **동일한 엔드포인트 계약**(`/jobs`, `/jobs/prompt`,
  `/jobs/{id}/events`)을 유지해 익스텐션 코드가 URL만 바꿔 양쪽에 붙게 한다.

---

## 4. 에이전트 연결: MCP 선택 근거

| 기준 | MCP | Ollama 프록시 |
|---|---|---|
| 마스킹의 성격 | 도구 호출(scan/mask)과 자연스럽게 일치 | chat 요청/응답 틀에 억지로 끼워야 함 |
| 파일 스캔 | `scan_file` 등으로 표현 가능 | chat API로 표현 불가 |
| 클라이언트 범위 | Claude Code/Desktop, Cursor 등 네이티브 지원 | Ollama 백엔드 클라이언트만 |
| 기존 검증 | `securedoc_mcp`로 동작 확인됨 | 신규 개발 |
| 향후 인젝션 LLM+MCP 계획 | 구조 일관 | 별개 구조 |

**MCP 도구 목록 (v1)** — 기존 `securedoc_mcp` 도구 세트 계승:

| 도구 | 역할 |
|---|---|
| `scan_text` | 텍스트 PII/인젝션 탐지 + 마스킹본 반환 |
| `scan_file` / `scan_files` | 파일/폴더 검사 (파서 경유) |
| `mask_text` | 탐지 목록으로 마스킹만 재적용 |
| `secure_read_file` | 정책 적용 파일 읽기 (마스킹본 반환) |
| `secure_search_files` / `secure_list_files` | 정책 적용 검색/목록 |
| `get_status` | 엔진 상태·정책·활성 detector 확인 |

**트랜스포트**: 엔진에 Streamable HTTP MCP 엔드포인트(`localhost:48200/mcp`)를 기본으로 하고,
stdio만 지원하는 클라이언트용으로 얇은 stdio shim(HTTP로 중계하는 런처 스크립트)을 함께 제공.
엔진 프로세스 하나를 모두가 공유하므로 상태·설정이 일원화된다(이력은 v1 스코프 밖, §2).

**인젝션 처리**: 인젝션 구간을 `[인젝션 마스킹]`으로 치환해 통과시키고,
탐지 내역(`injectionItems`)을 메타데이터로 함께 반환. **설정 팝업(§8, 좌하단
톱니바퀴)**에서 `mask | block` 정책 전환 가능 (기본 mask).

### 4.1 GPU 상주 정책 (모델 교체 이후, v1 룰베이스는 해당 없음)

GPU 모델은 VRAM에 로드된 동안만 점유하며, 로드/언로드 시점은 정책으로 제어한다.

| 모델 | 상주 방식 | 이유 |
|---|---|---|
| PII 인코더 | **항시 상주** | 크기가 작음(~0.5–2GB), 모든 요청에 걸리는 1차 검사라 지연 없이 즉답 필요 |
| 인젝션 LLM | **유휴 언로드** (기본 idle timeout 10분, 설정 팝업에서 조정) | 크기가 큼(~5GB), 상시 상주 시 다른 GPU 작업과 VRAM 경합 |

- **fail-closed 로드 대기**: 인젝션 LLM이 언로드된 상태에서 요청이 오면 검사를
  생략하지 않고 **모델 로드 완료까지 요청을 대기**시킨 뒤 검사한다. 콜드 스타트
  지연(수초~수십초)은 사용자에게 "인젝션 모델 준비 중" 진행 표시로 알린다.
  룰베이스 v1은 이 대기 자체가 없음(항상 즉시 사용 가능).
- **설정 팝업(§8, 좌하단 톱니바퀴)**에서 모델별 상주 방식(항시/유휴/즉시로드)과
  idle timeout을 개별 조정 가능.

### 4.2 "연결되면 모든 파일 검사" 보장

이 앱이 연결된 이상 검사를 우회할 수 있는 경로가 없어야 한다는 요구사항. 경로별 강제 방법:

| 경로 | 강제 방법 |
|---|---|
| 익스텐션 파일/프롬프트 인터셉트 | 네트워크 레이어 훅이라 우회 불가 (기존 구조 그대로) |
| MCP `secure_read_file` 등 게이트 도구 | 도구 자체가 항상 파이프라인을 통과하므로 우회 불가 |
| **MCP 클라이언트의 기본 파일 도구 (Read 등)** | **엔진이 강제할 수 없는 유일한 우회 경로.** MCP 설명서에 이미 명시된 문제 — MCP를 켜놔도 클라이언트가 자체 Read 도구로 원본을 읽으면 그냥 새어나간다 |

- v1 대응: 대시보드/설치 가이드에 "MCP 연결 시 클라이언트의 기본 파일 read 도구를
  끄고 `secure_read_file`만 허용" 안내를 **필수 설정 단계**로 노출 (설치 마법사에서
  체크리스트화, 단순 문서 각주로 두지 않음).
- **온보딩 AI의 자동 설정 가능 여부 (2026-07-22 조사, 6개 주요 MCP 클라이언트 기준)**:

  | 클라이언트 | 내장 파일 읽기 툴 | 설정파일로 자동 하드 차단 가능? | 메커니즘 |
  |---|---|---|---|
  | **Claude Code** | 있음 | **가능 (유일)** | `~/.claude/settings.json`(또는 프로젝트/로컬/enterprise managed-settings.json)의 `permissions.deny: ["Read"]`. 단 강제 버그 이력 있어(GH #24846 등) `PreToolUse` 훅 이중 방어 권장 |
  | **Claude Desktop** | **없음** (파일 접근은 사용자가 붙인 filesystem MCP 서버뿐) | 해당 없음 — "툴 차단"이 아니라 `claude_desktop_config.json`의 `mcpServers`에서 해당 서버 항목을 등록 안 하거나 삭제하는 방식 |
  | **Cursor** | 있음 | **불가능** | `permissions.json`엔 `mcpAllowlist`/`terminalAllowlist`/`autoRun`만 존재, 파일 읽기 차단 키 없음(공식 문서: 읽기는 승인 불필요) |
  | **Windsurf** | 있음 | **불가능(확인된 바 없음)** | `.codeiumignore`는 경로 차단(best-effort)일 뿐 툴 차단 아님 |
  | **Cline** | 있음 | **부분적(자동승인만 제어)** | `cline.autoApprove.readFiles:false`는 승인 프롬프트를 띄울 뿐, 사용자가 승인하면 그대로 읽힘 — 진짜 deny 아님 |
  | **VS Code Copilot Chat** | 있음 | **기본 Agent 모드에서 불가능** | 툴 제외는 사용자가 직접 고르는 커스텀 Chat Mode(`*.chatmode.md`)에서만 가능, 기본 모드엔 영속 설정 없음 |

  → 위 표는 **"설정 파일(permission/deny류) 자동 편집"** 한 가지 메커니즘만 봤을 때의
  결론이다 — 이 기준으로는 Claude Code만 가능. 아래 두 조사로 그림이 더 나뉜다.

- **경고(사후 감지) 가능 여부 — 로컬 로그 파싱 (2026-07-22 조사, 나머지 4개 클라이언트)**:

  | 클라이언트 | 로컬 로그 | 툴 호출 메타데이터 | 신뢰도 |
  |---|---|---|---|
  | **Cursor** | SQLite `state.vscdb`(`toolFormerData` 필드) | 있음(툴명·경로·승인여부) | 높음, 단 비공식 포맷이라 버전업 시 깨질 위험 |
  | **Windsurf(Devin Desktop)** | `state.vscdb` 자체는 툴콜 구조 미확인 | 확인 불가 | 낮음 — 대신 아래 공식 Hooks로 대체 |
  | **Cline** | 순수 JSON `api_conversation_history.json`(Anthropic 네이티브 tool_use 블록 그대로) | 있음, 가장 명확 | 가장 높음(오픈소스·평문·준실시간 append) |
  | **VS Code Copilot Chat** | `chatSessions/*.jsonl`(`toolInvocationSerialized`) | 있음 | 낮음 — 저장 누락 버그 다수 보고(GH #285535 등), 카운트 누락 위험 |

- **더 중요한 발견 — 공식 실시간 Hooks (로그 파싱보다 우선순위 높음)**: 4개 클라이언트
  전부 **`PreToolUse` 계열 공식 훅**을 이미 갖고 있어, 로그를 사후에 읽을 필요 없이
  **내장 Read 호출 자체를 실시간 차단**할 수 있다.
  - **Cursor**: `beforeReadFile`/`beforeMCPExecution` 훅(v1.7+)
  - **Windsurf/Devin Desktop**: Cascade Hooks `pre_read_code`/`pre_mcp_tool_use` — exit code 2로 실시간 차단 가능
  - **Cline**: `PreToolUse` 훅(v3.36+) — **단 macOS/Linux만 지원, Windows 미지원**
  - **VS Code**: Agent Hooks `PreToolUse` — **아직 Preview, 스펙 변경 가능성 있어 의존도 낮춰야 함**

  → **결론(정정)**: Cursor·Windsurf는 로그 파싱이 아니라 **공식 Hooks로 실시간 차단**을
  목표로 한다(§4.2 우회 방지 우선순위 1순위). Cline은 mac/linux는 Hooks, **Windows는
  Hooks 미지원이라 로그 파싱(`api_conversation_history.json`)이 실질적 대안**. VS Code는
  Hooks가 Preview라 로그 파싱을 당분간 병행. Claude Desktop은 여전히 해당 없음(§표 상단).
  Claude Code는 `permissions.deny` + 필요시 자체 `PreToolUse` 훅 이중 방어(기존 결정 유지).
  온보딩 AI의 자동 설정 범위는 **Claude Code(설정파일) + Cursor/Windsurf(공식 Hooks 등록)**
  까지 확장 가능 — 나머지(Cline/Windows, VS Code)는 로그 기반 사후 경고 + 수동 체크리스트.
- fail-closed 로드 대기(§4.1)와 이 우회 방지는 별개 축: 전자는 "검사가 느려도
  건너뛰지 않는다", 후자는 "검사 경로 자체를 피해가지 못하게 한다".

**온보딩 AI 구현 체크리스트 (2026-07-23 확정, Phase 6 완료 기준으로 사용)**:

| 클라이언트 | 온보딩 AI가 할 일 | 방식 |
|---|---|---|
| **Claude Code** | 설정 파일에 `permissions.deny: ["Read"]` 자동 추가 (diff 표시 → 사용자 확인 후 적용) + 가능하면 `PreToolUse` 훅도 함께 등록(이중 방어) | 설정파일 자동 편집 |
| **Cursor** | `beforeReadFile`/`beforeMCPExecution` 훅을 자동 등록해 내장 Read 호출을 실시간 차단 | 공식 Hooks 자동 등록 |
| **Windsurf/Devin Desktop** | Cascade Hooks `pre_read_code`/`pre_mcp_tool_use`를 자동 등록, exit code 2로 실시간 차단 | 공식 Hooks 자동 등록 |
| **Cline (macOS/Linux)** | `PreToolUse` 훅 자동 등록 | 공식 Hooks 자동 등록 |
| **Cline (Windows)** | Hooks 미지원 → `api_conversation_history.json` 파싱 기반 사후 경고 + 수동 체크리스트 안내 | 로그 파싱 + 수동 안내 |
| **VS Code Copilot Chat** | Agent Hooks(Preview) 등록 시도 + `chatSessions/*.jsonl` 파싱 병행(저장 누락 버그 있어 카운트 완전 신뢰 안 함) + 수동 체크리스트 | Hooks(불안정) + 로그 파싱 |
| **Claude Desktop** | 내장 읽기 툴 자체가 없음 — `claude_desktop_config.json`의 `mcpServers`에 서드파티 filesystem 서버가 등록돼 있는지만 확인해 있으면 경고 | 설정파일 확인(삭제는 사용자 승인 필요) |

Phase 6 완료 기준은 위 표의 7개 클라이언트 각각에 대해 명시된 방식이 실제로 동작하는지
확인하는 것으로 구체화한다(기존의 뭉뚱그린 "설치 가이드" 문구 대체).

---

## 5. Detector 모듈화 (모델 교체 대비)

```python
class Detection(TypedDict):
    type: str          # PERSON_NAME, EMAIL, ... / INSTRUCTION_OVERRIDE, ...
    start: int
    end: int
    text: str
    confidence: float
    source: str        # "encoder" | "llm"

class Detector(Protocol):
    name: str
    kind: Literal["pii", "injection"]
    async def detect(self, text: str, *, meta: ChunkMeta) -> list[Detection]: ...
```

- `detectors/registry.py`: 설정 파일에서 활성 detector 선택
  (`pii: encoder`, `injection: llm_mcp`). 룰베이스 폴백은 완전히 제거했다 — 가중치가
  아직 준비 안 됐으면 detector 자체가 아니라 파이프라인의 `model_status` 게이트가
  미검사 통과를 처리한다(§9.2).
- **구현**:
  - PII 인코더 모델 → `detectors/pii/encoder.py`, `Detection` 반환.
  - 인젝션 LLM+MCP → `detectors/injection/llm_mcp.py`. LLM 호출·MCP 클라이언트
    로직은 이 파일 안에 캡슐화.
- 탐지 유형 상수는 기존 파이프라인과 동일하게 유지:
  - PII: `PERSON_NAME / EMAIL / PHONE / ADDRESS / ID_NUMBER / CREDIT_CARD / DATE_OF_BIRTH / ORGANIZATION / BANK_ACCOUNT / OTHER_PII`
  - 인젝션: `INSTRUCTION_OVERRIDE / ROLE_MANIPULATION / SYSTEM_PROMPT_LEAK / JAILBREAK / HIDDEN_COMMAND / DATA_EXFILTRATION / OTHER_INJECTION`
- 룰베이스 v1: 정규식 룰 테이블(주민번호, 전화, 이메일, 카드번호, 계좌 등 +
  인젝션 패턴은 `프롬프트_인젝션_공격_유형.md` 기반). 룰은 데이터 파일(YAML/JSON)로
  분리해두되, (2026-07-23 정정) 대시보드 "룰 관리" UI는 v1 미지원(§8) — on/off·추가는
  당분간 이 YAML/JSON 파일 직접 편집으로만 가능.
- **언어 범위: 한국어만 (v1)**. 기존에 준비해둔 한국 PII 룰(주민등록번호 체크섬
  등)을 그대로 가져다 쓰되, 지금 단계는 정교화하지 않고 단순 이식 수준으로
  둔다. 영문권 패턴(국제 전화 형식 등)은 범위 밖 — encoder 교체 시점에
  재검토. 이메일 등 형식이 언어 무관인 룰은 그대로 유지.

---

## 6. 파서 (기존 계획 재사용)

`파이프라인/문서_파서_비교.md`의 결론 그대로:

| 포맷 | 파서 | 비고 |
|---|---|---|
| PDF (텍스트) | pdfplumber | 스캔 감지는 PyMuPDF로 사전 판별 |
| PDF (스캔) | **v1 제외** | MinerU는 GPU·170초/100p — "스캔 PDF는 미지원" 안내로 처리 |
| DOCX | python-docx | |
| HWPX | pyhwpx | |
| HWP | LibreOffice 변환 → python-docx | LibreOffice 미설치 시 미지원 안내 |
| XLSX | openpyxl | |
| PPTX | python-pptx | |
| TXT | 내장 | |

- 청크 분할(1,500자) 후 detector에 전달 — 기존 파이프라인 설계 유지.
- 마스킹 결과 파일 래핑은 기존 `docwrapper.js` 로직을 Python으로 이식
  (DOCX OOXML 빌드, PDF 입력도 `_masked.docx` 출력).

---

## 7. Chrome 익스텐션 (신규 제작, 기존 골격 이식)

**작업 위치 (2026-07-21 확정)**: 실제 구현은 이 문서와 같은 위치, 즉
`securedoc-gateway/extension/`에 새 하위 폴더로 작성한다. 기존 `파이프라인/extension/`은
아래 "유지(이식)" 대상의 **참고 원본일 뿐, 직접 수정하지 않는다** — 필요한 부분만
새 폴더로 포팅한다.

### 유지 (이식)
- `interceptor.js` MAIN world 훅 구조: `attachShadow` 훅, file input 차단,
  fetch/XHR 훅, 사이트별 설정 테이블(chatgpt/claude/gemini/copilot/grok/perplexity).
- MAIN ↔ Isolated ↔ SW 메시지 프로토콜 (`__securedoc` 플래그).

### 변경 1 — HITL: 팝업 모달 → 네이티브 사이드 패널
- `chrome.sidePanel` API. 화면 우측에 검사 진행·마스킹 미리보기·승인/취소 UI를 연다.
- **패널을 여는 호출 경로 (2026-07-23 실측 후 최종 정정)**: `content.js`(Isolated world)의
  document-level `click`/`keydown`/`submit`/`change` 리스너(제스처 그 자체)가
  `chrome.runtime.sendMessage({type:'OPEN_SIDE_PANEL', tabId})`를 **딱 한 번만** SW로
  보내고, **SW가 그 메시지 핸들러 안에서 추가 await 없이 곧바로**
  `chrome.sidePanel.open({tabId})`를 호출한다. **PII/인젝션 검사 자체(비동기, 시간이
  걸림)는 패널을 연 "이후에" 별도 메시지(START_SCAN)로 진행** — "패널 열기"(제스처
  필요)와 "검사 수행"(제스처 무관)을 분리하는 원칙은 유지하되, 패널을 여는 주체가
  content가 아니라 SW로 정정됐다.
  (하위호환: 파일 선택은 `interceptor.js`가 network layer에서 감지해 `postMessage`로
  content에 전달하는 경로도 있는데, 그 경우도 content가 받은 즉시 동일하게 SW에
  1회 위임한다 — 원칙은 "content→SW 메시지 왕복은 항상 1회"로 통일.)
- 패널 내용: 진행 단계 표시 → 원본/마스킹 diff 미리보기 → [마스킹본으로 전송] / [취소]
  (+ 인젝션 항목은 별도 색으로 구분).
- **Figma 디자인 확정 (2026-07-23, 파일 `hMO6k051z9JXBoa2fWySND` "지피티"/"지피티-문서
  입력 시작"/"지피티-대기" 프레임, #45:951 등)**: 이 3개 화면이 위 사이드패널 HITL의
  실제 디자인 스펙이다 (처음엔 익스텐션 디자인이 없는 줄 알고 별개로 해석했으나
  2026-07-23 사용자 확인으로 정정). 구성:
  - ChatGPT 웹페이지 목업(504px) + 가운데 "문서 검토" 패널(469px, 원본 diff — 계약서
    조항 텍스트에 PII는 **빨간** `MaskedSpan`, 프롬프트 인젝션은 **노란** `MaskedSpan`으로
    하이라이트) + 우측 "탐지 항목" 패널(195px, 이름/전화번호/이메일/주소/계좌번호/
    조직기밀/프롬프트 인젝션 등 항목별 on/off 토글, 상단에 "PII N건 | INJECTION N건 탐지"
    카운트, 하단 [전송]/[취소] 버튼).
  - "지피티-문서 입력 시작": 업로드 트리거 시점 화면.
  - "지피티-대기": 검사 진행 중 프로그레스 바 화면.
- 승인 결과는 SW 경유로 content → interceptor에 전달 (기존 프로토콜에
  `PANEL_DECISION` 메시지 추가) — 이건 제스처가 필요 없는 흐름이라 SW를 거쳐도 무방.
- **제스처 조사 결과 (2026-07-23 문헌조사 → 2026-07-23 실제 브라우저 실측으로 뒤집힘)**:
  2026-07-23엔 문헌조사만으로 "SW가 최종 호출하는 기존 방식은 Chromium 리그레션이
  잦은 취약한 패턴"이라 판단해 "content script가 `chrome.sidePanel.open()`을 직접
  호출"하는 쪽으로 설계를 바꿨었다(문서가 그 경로를 공식 지원한다고 읽었음).
  **2026-07-23, 실제 Chrome(149, Playwright로 익스텐션 로드해 진짜 클릭 이벤트로
  검증)에서 이 전제가 틀렸음이 확인됐다**: `chrome.sidePanel` 네임스페이스는
  **content script(Isolated world) 컨텍스트에 애초에 존재하지 않는다**
  (`typeof chrome.sidePanel === 'undefined'` 실측, `sidePanel` 권한은 정상 선언돼
  있었음에도). 즉 "content가 직접 호출"은 원천적으로 실행 불가능한 코드였고, 문서의
  "content script에서의 사용자 인터랙션" 문구는 "제스처가 content에서 시작돼도
  된다"는 뜻이지 "API 호출 자체를 content가 한다"는 뜻이 아니었다.
  반대로 실측 결과 **"SW가 메시지를 받은 그 자리에서(추가 await 없이) 곧바로
  `sidePanel.open()`을 호출하는, 메시지 왕복 1회짜리 경로"는 실제로 성공**했다
  (`sidePanel.open() SUCCESS`, 콘솔 로그로 확인 — Chromium 버그들이 지적하던 실패
  조건은 "메시지 왕복 2회 이상" 또는 "SW 핸들러 안에서 await/Promise를 거친 뒤 호출"
  이지, "1회 왕복 + 즉시 호출" 자체는 원래도 정상 동작하는 케이스였다).
  → **최종 설계(위로 반영 완료)**: SW 단일 홉 위임이 정식 1차 경로. "content 직접
  호출"이라는 선택지 자체를 코드에서 제거했다(`extension/content/content.js`,
  `extension/background/service-worker.js`, 2026-07-23).
  6개 사이트 전부에서의 실제 클릭 흐름 통합 테스트는 여전히 Phase 2 스파이크에서
  진행(이번 검증은 로컬 격리 테스트 페이지 기준 — 실제 사이트의 DOM/CSP가 이 메커니즘
  자체에 영향 줄 가능성은 낮지만 사이트별 셀렉터 정확성은 별도로 확인 필요).

**재정정 (2026-07-24) — `chrome.sidePanel` API 폐기, 페이지 내 iframe 오버레이로 전환**:
실사용 중 이 API 자체의 구조적 한계 두 가지가 확인됐다 —
(1) 탭 스코핑 불완전: `setOptions({tabId, enabled:false})`는 "그 탭에서 다시 열 수
없게" 만들 뿐, 이미 창에 도킹되어 열린 패널을 강제로 닫는 `close()` 같은 API가
아직 없다(W3C webextensions #521에서 계속 요청 중인 미구현 기능:
https://github.com/w3c/webextensions/issues/521). (2) manifest의
`side_panel.default_path`가 "모든 탭에 기본으로 열린 전역 패널 인스턴스"를 만들어
탭별 차단과 근본적으로 충돌한다(https://pmds.info/blog/chrome-extension-side-panel-per-tab).
이 둘을 여러 겹으로 우회해봤지만(전역 disable, 선제적 다른 탭 비활성화, 세션
브로드캐스트 tabId 스코핑 등) "다른 탭으로 넘기면 이미 열린 패널이 물리적으로
완전히 닫힌다"는 크롬이 close() 계열 API를 추가하기 전까진 확장 코드만으로
강제할 수 없는 플랫폼 한계였다.

**최종 설계**: `chrome.sidePanel` 전체를 걷어내고, 검토 패널을 `content.js`가 이
탭의 페이지 DOM에 **직접 iframe으로 주입**하는 방식으로 바꿨다(`src`는
`chrome.runtime.getURL('sidepanel/sidepanel.html')`, `position:fixed; right:0;
width:560px; height:100vh; z-index:2147483647`로 우측 도킹). 기존 sidepanel.html/
css/js는 그대로 재사용(iframe도 chrome-extension:// 오리진이라 `chrome.runtime`
메시징이 동일하게 동작). 이 방식의 이점:
  - iframe은 물리적으로 그 탭의 DOM 안에만 존재 → 다른 탭엔 애초에 나타날 수
    없다(탭 스코핑 문제 자체가 소멸, enabled/disabled 관리 전부 불필요).
  - 폭/높이를 완전히 우리가 통제(브라우저가 정하는 사이드패널 독 폭에 종속되지 않음).
  - DOM에서 제거하면 확실하게 닫힌다(닫기 API 부재 문제가 없음) — 상단바에 X
    버튼을 추가해 진행 중에도 수동으로 닫을 수 있게 했다(사이드패널엔 있던
    네이티브 닫기 버튼이 없어졌으므로).
  - DOM 삽입 자체엔 사용자 제스처가 필요 없어, "SW가 메시지 핸들러 안에서
    await 없이 곧바로 호출해야 제스처가 보존된다"는 위 문단의 제약 자체가
    사라졌다 — content.js가 제스처 시점에 동기적으로 직접 주입한다(SW 왕복 불필요).
  - manifest에서 `sidePanel` 권한과 `side_panel.default_path`를 제거, 대신
    `web_accessible_resources`에 `sidepanel/*`를 노출해 페이지 컨텍스트에서
    iframe으로 로드 가능하게 함.
  Playwright로 실제 트러스티드 클릭 제스처를 통한 전체 플로우(오버레이 주입 →
  실제 엔진 스캔 → 결과 렌더 → X 버튼으로 닫기)를 검증, tab A에서 연 오버레이가
  tab B의 DOM엔 전혀 존재하지 않음을 확인.

### 변경 2 — 익스텐션 popup: 설정 전용으로 재도입 (2026-07-23 최종 확정)

**경위**: 2026-07-21에 "popup 축소" 계획을 한 번 폐기했었다 — 당시 Figma의 트레이
팝업(보호 토글·모델 상태·시스템 리소스)을 익스텐션 popup으로 착각했기 때문(실제로는
Electron 앱의 macOS 메뉴바 트레이, §8). 그래서 "사이드패널에 설정 탭 통합"으로
바꿨었는데, 2026-07-23 Figma 완성본을 재검토하며 사이드패널의 실제 디자인(§변경1의
"지피티" 프레임들)엔 탭 스위처 UI가 아예 없다는 게 확인됐고, 이를 계기로 **설정
UI를 아예 다시 popup으로 분리**하기로 최종 확정했다. 이번엔 착각이 아니라 명시적
재결정이다.

**최종 구조**:
- **사이드패널**: HITL 전용, 탭 없음. **평소엔 존재하지 않는다(닫힌 상태)** —
  파일/프롬프트 마스킹 결과가 돌아왔을 때만 `sidePanel.open()`으로 열림(§변경1).
  설정 UI는 여기 없다.
- **popup**: 설정 전용으로 재도입. **우측 상단 버튼**(브라우저 툴바의 익스텐션
  아이콘, `action.default_popup`) 클릭 시 뜬다. `extension/popup/`을 다시 만들되
  내용은 예전(보호 토글·범위 카드·최근 활동 카드)과 다르게 아래로 한정:
  - 서버 설정: 로컬 포트 확인, 현재 연결 대상 표시(로컬/원격) — **원격 URL 편집은
    없음**(2026-07-23 결정: 원격 URL은 앱 설정 팝업, §8에만 존재).
  - 앱 설정: 엔진(로컬 앱) 연결 상태, 대시보드 열기 버튼, 앱 미설치 시 안내.

### 변경 3 — 프롬프트 인터셉트 (2026-07-21 재확인: 이미 구현됨)
- (당초 계획대로) 전송 버튼 클릭/Enter 시점에 입력창 텍스트 캡처 → `/jobs/prompt` 검사 →
  사이드 패널 승인 → 마스킹 텍스트로 치환 후 전송.
- **실제 구현은 `extension/content/interceptor.js`에 별도 `PROMPT_CONFIGS` 테이블로
  존재** (당초 계획한 "기존 사이트 테이블에 `promptHook` 필드 확장"이 아니라
  `SITE_CONFIGS`와 분리된 전용 테이블, 필드는 `editorSel`/`sendBtnSel`/`editorType`).
  6개 사이트(chatgpt/claude/gemini/grok/perplexity/copilot) 전부 등록 완료.
- 클릭·Enter 캡처(capture 단계) → `_interceptPromptSubmit` → `window.postMessage`로
  MAIN↔Isolated 브리지 통해 content.js에 처리 요청 → 사이드패널 승인 결과
  (`masked`/`passthrough`/`cancel`) 수신 → `_setEditorText` + `_reSubmitPrompt`로
  치환 후 재전송. 타임아웃(10분) 시 자동 취소.
- 따라서 Phase 3는 "신규 구현"이 아니라 **6개 사이트 전체에 대한 회귀 테스트·안정화
  확인** 작업으로 범위 재정의.

---

## 8. Electron 대시보드 앱

- **역할**: 엔진 사이드카 관리(spawn·재시작·포트)·대시보드 UI·트레이 상주.
  MCP 어댑터(§2·§4)는 엔진에 구현되어 있고, 앱은 그 엔진이 로컬에서 항상 켜져
  있도록 실행·관리만 한다 — 즉 앱은 "다른 AI 에이전트의 MCP 연결이 로컬에서
  가능하도록 게이트웨이(엔진)를 띄워주는" 역할이며, MCP 프로토콜 자체를 구현하지 않는다.
- **화면 (2026-07-23 Figma 완성본 기준 전면 교체)**: 사이드바 메뉴는 4개뿐이다
  (기존에 적었던 "이력/통계/룰 관리/설정" 5화면 구조는 Figma 어디에도 없어 폐기).
  1. **홈** (`30:483`) — UpSecurity 로고, PII model/Injection model 상태 pill,
     "Safety User" 계정 푸터.
  2. **연결** (`31:1585`) — MCP CLI 카드(온보딩 명령 코드블록 3줄 중 **1줄만 확정**
     `npx upsecurity-mcp connect`, 나머지 2줄은 Figma에 `ollama launch chatgpt` 같은
     플레이스홀더 텍스트가 남아있어 디자인 미완성 — **(2026-07-23 결정) 일단 지금
     Figma 디자인 그대로 제작하고, 나중에 디자인이 확정/변경되면 그때 코드도 맞춰
     바꾼다**(선반영 후 추후 수정 방식), 연결됨/미연결 상태 뱃지) + Chrome Extension
     카드("브라우저에서 사용하는 AI 서비스를 안전하게 보호합니다", "확장 프로그램
     설치하기 →", "더 많은 연동 방식이 곧 추가됩니다").
     - **연결됨/미연결 판정 기준 (2026-07-23 확정): 활성 세션 기준.** MCP는 현재
       활성 MCP 세션(Streamable HTTP 세션 또는 attach된 stdio 프로세스)이 있는지로
       판정 — 단순히 "최근 N분 내 호출 이력"이 아니라 세션이 열려 있는지를 본다.
       Chrome Extension은 익스텐션 background(service worker)가 엔진과 맺은 활성
       연결(예: 최근 `/health` 성공 + 살아있는 포트 연결)이 있는지로 판정.
  3. **대시보드** (`31:1665`) — 통계가 별도 화면이 아니라 **이 화면에 통합**:
     PII/INJECTION 탐지량 일/주/월(예: 11/12/23건) + 누적(예: 46건) + 추이 AreaChart,
     모델 상태, 시스템 리소스.
  4. **처리현황** (`31:1746`, 대기 상태 / `51:598`, 실행중 상태) — 파이프라인 플로우
     시각화(메시지 수신 → 텍스트/OCR 추출 → PII 탐지 → 마스킹 완료 / INJECTION 탐지 →
     차단 완료). "요소를 드래그해 위치를 조정해보세요" 힌트 있음 — **(2026-07-23 확정)
     단순 디자인 표현이 아니라 실제 구현할 기능**: 사용자가 플로우 노드 위치를
     드래그로 재배치하고 그 배치를 저장/유지하는 인터랙션을 만든다.

  **확정 (2026-07-23)**:
  - **이력**: v1에서 완전히 제거. job 목록/원본-마스킹 diff 화면 자체를 만들지 않는다.
  - **룰 관리**: v1 미지원으로 남긴다(화면도, 편집 UI도 없음) — 룰 on/off·추가는
    당분간 룰 YAML 파일 직접 편집으로만 가능, 추후 지원 검토.
  - **설정**: 사이드바 메뉴가 아니라, **좌하단 톱니바퀴 아이콘**(Claude Desktop 설정
    버튼과 동일한 위치·상호작용 패턴)으로 추가한다. 클릭 시 팝업(모달)으로 뜬다.
    내용 (2026-07-23 확정):
    - **인젝션 정책** (mask/block 전환, 기본 mask) — v1에서 실제로 동작하는 유일한 항목.
    - **원격 URL** (익스텐션엔 없고 여기에만 있음, §3/§변경2).
    - detector 선택(모델 교체 슬롯), **GPU 상주 정책(모델별 상주 방식·idle timeout)**은
      모델 교체 이후에만 의미 있는 항목 — v1 룰베이스에선 no-op이라 **모델이 붙기 전엔
      숨기거나 비활성 표시**(§4.1/§5). v1 설정 팝업은 사실상 인젝션 정책 + 원격 URL만.
    - **포트는 넣지 않는다** (2026-07-23 확정): 엔진이 완전 자동 관리(48200~48209 스캔,
      §11)하므로 앱 설정에서도 편집 항목으로 두지 않는다. 앱은 자기가 띄운 엔진의
      실제 포트를 알고 있으니 필요하면 읽기 전용 표시만 가능.
    - Figma에 아직 이 톱니바퀴/설정 팝업 프레임은 없으므로 디자인은 추후 확정 —
      위치·인터랙션 패턴·내용 목록만 지금 확정.
- **트레이(메뉴바) 팝업** (2026-07-21 Figma 확인, 파일 `hMO6k051z9JXBoa2fWySND` 노드 `36:2846`
  "현황"): macOS 메뉴바 아이콘 클릭 시 드롭다운으로 표시. 구성 요소:
  - **Security ON/OFF 토글** (초록 on 상태)
  - **PII model / INJECTION model** 상태 필(pill)
  - **CPU / GPU / RAM / VRAM** 리소스 바 + 퍼센트(시스템 모니터링)
  - **"PII 탐지 | 오늘 N건" / "INJECTION 탐지 | 오늘 N건"** 카운트
  - **"대시보드에서 더 자세히 보기 →"** 링크(대시보드 홈으로 이동)
  - **"QUIT UpSecurity"** 버튼
  - 크기 약 256×298, 라운드 코너 + 블러 배경(macOS 네이티브 팝오버 스타일).
- k-harness에서 재사용: 트레이 상주 패턴, 전역 단축키, electron-builder 패키징 설정.
- 엔진과의 통신도 동일 REST/WebSocket(이벤트) 사용 — 별도 IPC 프로토콜 안 만듦.
- **자동 업데이트**: `electron-updater`로 앱만 자동 갱신(Phase 4에서 구성).
  엔진(Python 사이드카)은 앱과 함께 배포되는 리소스이므로 앱 업데이트에 편승.
  익스텐션은 v1에서 수동 배포 가정(Chrome 웹스토어 미등록) — 새 버전은
  압축 해제 폴더 교체 방식으로 안내.

---

## 9. 프로젝트 구조

```
securedoc-gateway/
├ engine/                  # Python
│  ├ pyproject.toml
│  ├ app/
│  │  ├ main.py            # FastAPI 엔트리
│  │  ├ adapters/
│  │  │  ├ http_api/       # REST (기존 EC2 계약 호환)
│  │  │  └ mcp/            # Streamable HTTP + stdio shim
│  │  ├ core/
│  │  │  ├ parser/
│  │  │  ├ detectors/{pii,injection}/
│  │  │  ├ masker/
│  │  │  └ pipeline/
│  │  ├ store/             # SQLite, 설정, audit.log
│  │  └ rules/             # 룰 데이터 (YAML)
│  └ tests/
├ desktop/                 # Electron
│  ├ package.json
│  ├ main/                 # 창·트레이·사이드카 관리
│  └ renderer/             # 대시보드 UI
├ extension/               # Chrome MV3
│  ├ manifest.json         # + sidePanel 권한 + action.default_popup
│  ├ background/
│  ├ content/              # interceptor.js(이식) + content.js
│  ├ sidepanel/            # HITL 전용, 탭 없음. 평소엔 닫혀있고 결과 반환 시에만 열림
│  └ popup/                # 설정 전용 (§변경2, 2026-07-23 재도입) — 우측 상단 버튼으로 오픈
└ docs/
   └ PLAN.md (이 문서)
```

### 9.1 원본 텍스트 보관 정책 (`store/`)

- HITL 승인 대기 중에만 원본이 **인메모리**(job 객체)에 존재. 승인/취소로 job이
  종료되면 원본 참조를 즉시 폐기(GC 대상화), 디스크에 쓰지 않음.
- `store/`(SQLite)에는 job 메타데이터만 영속화: 파일명, 처리 시각, 탐지 유형별
  개수, 각 탐지의 `start/end`+`type`(마스킹 후 텍스트 스니펫은 남겨도 됨,
  **원문 스니펫은 저장 금지**), 소스(익스텐션/MCP), 정책 결정 결과.
- (2026-07-23 정정) 대시보드에 "이력" 화면 자체가 없어졌으므로(§8) 원본-마스킹 diff
  뷰는 **HITL 사이드패널의 그 순간(승인/취소 시점)에만** 보여준다. job이 끝나면
  원본은 즉시 폐기되고, `store/`엔 마스킹본+탐지 유형·위치만 남으므로 사후에
  diff를 다시 볼 수 있는 화면 자체가 없다 — "과거 이력 조회"라는 유스케이스가
  이번 화면 개편으로 아예 사라졌음을 인지하고 넘어갈 것.
- `audit.log`(JSONL)도 동일 원칙: 이벤트 메타데이터만 기록, 원문 텍스트 미포함.

### 9.2 파싱 실패/미지원 포맷/타임아웃 처리 — 통일된 "미검사 통과" 정책

아래 세 가지 상황을 **동일한 정책**으로 처리한다: **경고 후 통과 허용**
(사용성 우선). 셋 다 "연결되면 모든 파일에 탐지 적용" 원칙의 **명시적
예외**이므로 다음을 공통으로 필수 구현:

| 상황 | 예 |
|---|---|
| 파싱 실패 | 손상된 파일, 암호화된 문서 |
| 미지원 포맷 | 스캔 PDF, 변환기 없는 HWP |
| 엔진 무응답/타임아웃 | 엔진 다운, 네트워크 지연, 콜드 스타트 장기화 |

- HITL/사이드 패널에 굵은 경고 표시, 사유를 구분해 안내:
  "이 파일은 검사되지 않았습니다 (사유: 스캔 PDF / 파서 오류 / 서버 응답 없음 등)"
- 통과 결정을 **store DB에** `scan_status: "failed" | "unsupported" | "timeout"`로
  남겨 감사 가능하게 함(§9.1 원칙과 마찬가지로 원문은 남기지 않되, "검사 안 됨"
  사실 자체는 기록).
- (2026-07-23 확정) **"미검사 통과" 통계의 대시보드 UI 노출은 v1에서 드롭** — store
  DB에 `scan_status`로 기록만 하고, 대시보드 화면(§8, Figma 기준 PII/INJECTION
  탐지량만 표시)엔 별도 섹션을 두지 않는다. `timeout` 비중이 높으면 엔진 안정성/GPU
  상주 정책(§4.1) 재검토 신호, `unsupported` 비중이 높으면 파서 커버리지 확장(Phase 5)
  신호라는 분석 가치는 여전하므로, UI 노출은 추후(모델 교체·화면 확장 시점) 재검토.
- 타임아웃 임계값은 설정 가능한 값(기본값 예: 요청당 30초)으로 두어, 콜드
  스타트(§4.1)로 인한 정상 지연과 실제 무응답을 구분한다.

---

## 10. 단계별 로드맵

| Phase | 내용 | 완료 기준 |
|---|---|---|
| **0** | 엔진 스캐폴딩: pipeline + 룰베이스 detector + 파서(TXT/PDF/DOCX) + REST API | `curl /jobs`로 파일 넣으면 마스킹 결과 반환 |
| **1** | MCP 어댑터 (HTTP + stdio shim) | Claude Code에서 `scan_text`/`scan_file` 동작 |
| **2** | 익스텐션: 인터셉터 이식 + **사이드 패널 스파이크(제스처 검증) 최우선** + 파일 HITL + 설정 전용 popup 신설(§변경2) + 로컬/AWS 자동 전환. **6개 사이트(chatgpt/claude/gemini/copilot/grok/perplexity) 동시 지원** | 6개 사이트 전부에서 파일 업로드 시 패널 승인 흐름 완주 |
| **3** | 프롬프트 인터셉트 — `interceptor.js`에 이미 구현됨(§변경 3), 6개 사이트 회귀 테스트·안정화만 남음 | 6개 사이트 전부에서 프롬프트 마스킹 후 전송 동작 |
| **4** | Electron 대시보드(홈/연결/대시보드/처리현황 4화면) + 좌하단 설정 팝업 + 트레이 + 사이드카 + 패키징 | 인스톨러 하나로 엔진+앱 설치, 대시보드에 통계 표시 |
| **5** | 파서 확장 (HWP/HWPX/XLSX/PPTX) — 룰 관리 UI는 v1 미지원으로 스코프 제외 | 전 포맷 검사 |
| **6** | 모델 교체 슬롯 검증: encoder/llm_mcp 스텁 붙여 전환 테스트 + GPU 상주 정책(§4.1) 적용 + MCP 우회 방지 온보딩 AI, 클라이언트별 체크리스트(§4.2) | 설정 전환만으로 detector 교체 확인, idle unload/fail-closed 대기 동작 확인, §4.2 체크리스트 7개 클라이언트 전부 명시된 방식대로 동작 확인 |

---

## 11. 리스크 및 메모

- **sidePanel 제스처 제약**: (2026-07-23 실측 완료, §변경1 참고) `chrome.sidePanel`이
  content script에 없다는 걸 실제 Chrome 검증으로 확인 → SW 단일 홉 위임(메시지 1회,
  SW가 즉시 호출)이 정식 경로로 확정, 코드 반영 완료. 배지+토스트 폴백은 (툴바 아이콘이
  설정 popup 전용이라 애초에 쓸 수 없어서) 필요 없음 — 이번 실측으로 실패 가능성 자체가
  낮아짐. 남은 건 6개 실사이트에서의 셀렉터/DOM 통합 테스트뿐(Phase 2 스파이크).
- **LibreOffice 의존**: 앱에 번들하면 설치 용량 급증 → 미설치 시 HWP만 미지원 안내로 처리.
  (2026-07-21 재확인) 정확한 HWP 점유율 통계는 확인 못 했으나, 2026-04~05 정부가
  On-Nara 시스템 기준 HWP→HWPX 전환을 공식화(지방정부까지 확대)한 상황이라 향후
  "LibreOffice 없이는 못 여는 순수 HWP" 비중은 줄어드는 흐름 — 결정 변경 불필요.
  다만 실측 없이 내린 판단이므로 §9.2 `scan_status`로 HWP 미지원 발생 빈도를 계측해
  규모가 크면 재검토.
  **(2026-07-23 U5 구현 중 정정) "HWPX는 pyhwpx로 번들 없이 지원 중"이라는 전제가
  틀렸음을 발견** — pyhwpx(1.7.2)는 순수 파싱 라이브러리가 아니라 `win32com`으로
  **실제 한컴오피스(한/글) 앱을 COM 자동화**하는 방식(`HWPFrame.HwpObject`)이다.
  즉 HWPX도 HWP(LibreOffice)와 마찬가지로 **최종 사용자 PC에 외부 프로그램(한컴오피스)이
  설치돼 있어야 동작**하고, 미설치 시 COM Dispatch 실패 → `unsupported`로 우아하게
  처리되도록 구현했다(실제 검증: 이 개발 환경엔 한컴오피스가 설치돼 있어 실제 hwpx
  파싱·PII탐지까지 성공 확인, LibreOffice는 미설치라 HWP는 unsupported 경로만 확인).
  한컴오피스 미설치 환경에서의 HWPX 지원 비율이 예상보다 낮을 수 있음 — HWP와 마찬가지로
  `scan_status`로 발생 빈도 계측 필요. Windows 전용 방식(win32com)이라 macOS/Linux
  지원 여부도 별도 확인 필요(HWPX 자체는 개방형 포맷이라 향후 non-COM 순수 파서로
  교체할 여지 있음 — 이번엔 빠른 이식을 위해 pyhwpx 그대로 사용).
- **포트 충돌 — 스캔 범위·알고리즘 구체화 (2026-07-23 확정)**:

  **엔진 쪽 (bind 로직)**:
  - `BASE_PORT = 48200`부터 시도, `EADDRINUSE` 시 `+1`씩 증가해 **최대 10개
    포트(48200~48209)**까지 순차 시도.
  - 10개 전부 점유돼 있으면(사실상 발생 거의 불가능한 케이스 — 이전 크래시로 남은
    좀비 프로세스 정도) 앱 시작을 실패시키고 명확한 에러 메시지 표시(사용자에게
    "포트 48200~48209가 모두 사용 중입니다" 안내 + 재시도/프로세스 정리 안내).
  - `/health` 응답에 실제 바인딩된 포트 번호와, **엔진임을 식별하는 고정 시그니처
    필드**를 함께 반환: `{"service": "securedoc-gateway", "port": <실제포트>, ...}`.
    시그니처가 필요한 이유: 익스텐션이 스캔하는 포트 범위에 사용자의 다른 로컬
    개발 서버(예: 48201에 떠 있는 무관한 앱)가 우연히 있을 수 있으므로, 단순
    200 응답만으로는 오판할 수 있음 — 반드시 이 필드까지 일치해야 "우리 엔진"으로 인정.

  **익스텐션 쪽 (탐지 로직)**:
  - 48200~48209 **10개 포트에 병렬로** `GET /health` 요청(각 500ms 타임아웃) —
    순차 스캔이 아니라 병렬이라 최악의 경우(엔진이 아예 안 켜져 있어 전부 타임아웃)도
    총 소요 시간은 여전히 ~500ms로, 기존에 명시된 단일 포트 타임아웃과 동일한 체감.
  - 응답 중 `service: "securedoc-gateway"` 시그니처가 일치하는 첫 번째 포트를 채택.
    (이론상 동시에 여러 포트가 응답할 일은 없지만, 방어적으로 가장 낮은 포트 번호를
    우선한다.)
  - 발견한 포트는 익스텐션 로컬 상태(`chrome.storage.session` 등)에 캐싱해 매 요청마다
    재스캔하지 않는다. 단, 캐시된 포트로 요청이 실패하면(엔진 재시작으로 포트가
    바뀌었을 가능성) **그 즉시 위 10-포트 스캔을 재실행**해 새 포트를 다시 찾는다 —
    별도의 주기적 폴링은 두지 않고 "요청 실패 시에만 재스캔"으로 단순화.
  - 설정 popup(§변경2)엔 이 캐싱된 탐지 결과(포트 번호, 연결 대상)를 읽기 전용으로
    표시만 하고, 사용자가 고칠 수 있는 입력란은 두지 않는다.
- **스캔 PDF**: v1 미지원 (MinerU는 GPU 필요·느림). 스캔 감지 시 명확한 안내 메시지.
- **REST 계약 호환**: 로컬 엔진이 기존 EC2 API와 같은 계약을 지켜야
  익스텐션 폴백이 코드 분기 없이 동작 — 계약 테스트로 보장.
- **MCP 우회 경로**: 클라이언트의 기본 파일 read 도구는 엔진이 강제로 막을 수
  없음(§4.2). "전 파일 검사"를 실제로 보장하려면 설치 가이드의 클라이언트
  권한 설정 단계가 빠지면 안 됨 — 문서 각주가 아니라 설치 마법사 체크리스트로.
- **인젝션 LLM 콜드 스타트**: 유휴 언로드 정책상 첫 요청은 로드 대기가 걸림
  (§4.1). fail-closed로 검사를 절대 생략하지 않되, 사용자에게 "모델 준비 중"
  진행 표시가 없으면 앱이 멈춘 것처럼 보일 수 있어 UX 필수 요소로 취급.
- **파싱 실패 시 미검사 통과(§9.2)**: "모든 파일 탐지" 원칙과 상충하는 의도적
  예외. 통계 지표로 노출해 규모가 커지면 재검토.
- **대시보드 무잠금**: 로컬 단일 사용자 전제가 깨지는 환경(공유 PC, 발표 데모
  중 화면 공유 등)에서는 탐지된 PII 종류·문서명이 그대로 노출됨 — 데모/시연
  시 대시보드 화면(§8) 노출에 주의.
- **원본 미저장 → 사후 diff 불가(§9.1)**: "이력" 화면 자체가 없어졌으니(§8) 더욱
  확고해진 제약 — 디버깅/오탐 검증 시 "그때 정확히 어떤 텍스트였는지" 재현이 안
  됨. 오탐 신고 기능은 마스킹본 기준으로만 설계.
- **6개 사이트 동시 지원**: 사이트별 DOM/업로드 방식 차이로 Phase 2~3 개발·QA
  범위가 1개 사이트 대비 커짐 — 사이트 하나가 깨져도 나머지가 막히지 않게
  사이트별 독립 에러 처리 필수.
