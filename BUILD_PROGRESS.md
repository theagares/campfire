# securedoc-gateway 빌드 진행상황 (자동 빌드 추적 파일)

> 이 파일은 서브에이전트 기반 자동 빌드의 **재개용 상태 파일**이다. 컨텍스트가
> 리셋돼도 이 파일을 읽으면 어디까지 했고 다음에 뭘 할지 알 수 있다.
> 규칙: 각 유닛 완료 시 상태를 `[ ]`→`[x]`로 바꾸고, "마지막 완료/다음 할 일"을 갱신한다.

## 기준 문서
- 계획: `securedoc-gateway/PLAN.md` (이게 유일한 사양 소스)
- 이식 원본(참고만, 직접 수정 금지): `파이프라인/server/`, `파이프라인/securedoc_mcp/`, `파이프라인/extension/`
- 참고 문서: `파이프라인/문서_파서_비교.md`, `파이프라인/프롬프트_인젝션_공격_유형.md`

## 핵심 원칙 (PLAN 요약)
- v1 탐지 = **룰베이스 한국어만**. 모델(encoder/LLM)은 Detector 인터페이스로 향후 교체(§5).
- 엔진 구조 §9: `engine/app/{main.py, adapters/{http_api,mcp}, core/{parser,detectors,masker,pipeline}, store, rules}`
- 포트: 48200부터 48209까지 자동 스캔/바인딩, `/health`에 `{"service":"securedoc-gateway","port":N}` 시그니처(§11).
- 원본 텍스트 영속 저장 금지, store엔 메타데이터+scan_status만(§9.1/§9.2).

## 빌드 유닛 (순서대로)

- [x] **U1 — 엔진 코어 (Phase 0)** ✅ 완료·검증(2026-07-23): `engine/` 30개 py파일, pytest 31개 통과,
      서버 기동 시 48200 점유→48201 자동폴백 확인, /health 시그니처 정상, /jobs 마스킹 반환 확인.
      룰베이스 PII(주민번호 체크섬·카드 Luhn·전화·이메일·계좌·이름·주소·생년월일·조직) +
      인젝션 6유형. Detector Protocol/registry 구현. TXT/PDF/DOCX 파서(HWP/HWPX/XLSX/PPTX는 스텁→U5).
      실행: `cd engine && ./.venv/Scripts/python.exe -m app.main` (테스트 `-m pytest -q`).
- [x] **U2 — MCP 어댑터 (Phase 1)** ✅ 완료·검증(2026-07-23): `engine/app/adapters/mcp/`(tools.py/stdio_shim.py/__init__.py).
      FastMCP Streamable HTTP를 FastAPI `/mcp`에 마운트(엔진 프로세스 공유), 도구 8종 전부 U1 core 호출.
      tools/list + scan_text/scan_file/get_status HTTP·stdio 양쪽 검증. pytest 33개 통과(회귀 없음).
      미검증: 실제 Claude Desktop/Code 앱 UI 연결(SDK 클라이언트로만 검증). 시크릿 전용 룰은 향후 detector 슬롯.
- [x] **U3 — 익스텐션 (Phase 2·3)** ✅ 완료·검증(2026-07-23): `extension/`(manifest MV3, background/SW+config,
      content/interceptor(verbatim 이식)+content, sidepanel/(HITL Figma), popup/(설정전용), utils, tests).
      §변경2 설정popup + §변경3 6개 사이트 PROMPT_CONFIGS 이식. manifest MV3 valid, JS 9개 문법 통과, 회귀테스트 통과.
      ⚠️ **보안조치**: SW config.js에 하드코딩됐던 가짜 bearer 토큰 제거함(계획에 원격인증 사양 없음).
      🔬 **2026-07-23 실브라우저 검증 완료 → PLAN.md §변경1 정정**: Playwright로 실제 Chrome(149)에
      익스텐션을 로드해 sidePanel.open() 제스처 체인을 검증한 결과, "content.js가 직접 호출"이라는
      원래 1순위 경로가 **원천적으로 불가능**함을 확인(`chrome.sidePanel`이 content script 컨텍스트에
      아예 없음, `typeof === 'undefined'` 실측). 반대로 "SW 단일 홉 위임"(메시지 1회 + SW가 즉시 호출)이
      실제로 성공함을 확인 → `content.js`/`service-worker.js`를 이 경로 하나로 정정 완료, 문법·회귀테스트
      재확인 통과. 6개 실사이트에서의 셀렉터/DOM 통합 테스트는 여전히 Phase 2 스파이크로 남음.
- [x] **U4 — Electron 앱 (Phase 4)** ✅ 완료·검증(2026-07-23): `desktop/`(main/engine-manager.js 등 12개,
      renderer/index.html+app.js+charts.js, scripts/). npm install 성공, 문법검사 19/19 통과, 엔진 사이드카
      스모크 실제 실행 성공(spawn→포트48200 탐지→/health 시그니처 수신→종료). 하드코딩 시크릿 스캔 클린.
      4화면(홈/연결/대시보드[통계 실측 SQLite]/처리현황[드래그 재배치+저장]) + 설정팝업(인젝션정책+원격URL만
      활성, detector/GPU상주는 비활성표시, 포트편집 없음) + 트레이 팝오버 구현.
      ⚠️ **미검증**: 실제 앱 창 렌더링/트레이 표시/드래그 UI/electron-builder 패키징 빌드(GUI 환경 필요).
      **gap**: 엔진에 활성세션 조회 API가 없어 "연결됨" 판정이 근사치(익스텐션 연결은 unknown 표시) — U6나
      추후 엔진 개선 필요.
- [x] **U5 — 파서 확장 (Phase 5)** ✅ 완료·검증(2026-07-23): xlsx.py/pptx.py/hwpx.py/hwp.py 추가.
      XLSX/PPTX는 실제 파일 생성→파싱→PII탐지·마스킹 실증. HWPX는 이 환경에 한컴오피스 설치돼있어
      실제 hwpx 생성→파싱→PII탐지 실증. HWP는 LibreOffice 미설치라 unsupported 우아한 처리만 검증
      (실변환 미검증). pytest 40개 통과(회귀 없음).
      ⚠️ **중요 발견 → PLAN.md §11 갱신함**: pyhwpx는 순수 라이브러리가 아니라 win32com으로 실제
      한컴오피스를 구동하는 방식 — HWPX도 HWP처럼 외부 프로그램 의존(Windows전용). PLAN의
      "번들 없이 지원" 전제 정정.
- [x] **U6 — 모델 교체 슬롯 + MCP 우회 방지 (Phase 6)** ✅ 완료·검증(2026-07-23, 마지막 유닛):
      encoder/llm_mcp detector 스텁(registry 전환 검증) + GPU 상주 정책(gpu_residency.py — idle
      unload/fail-closed 로드대기 실제 타이밍 검증) + 온보딩 체크리스트 도구(`app/onboarding/`,
      7클라이언트: Claude Code/Cursor/Windsurf/Cline mac·linux/Cline Windows/VS Code/Claude Desktop).
      pytest 74개 통과(회귀 없음).
      ⚠️ **안전검증**: 온보딩 도구가 이 세션의 실제 `~/.claude/settings.json`을 건드렸는지 직접 대조
      확인 — 내용은 `/model` 명령들로 인한 정상 변경(model/theme/effortLevel)뿐, permissions.deny나
      _securedocGateway 마커 없음 → **실제 홈 설정 미접촉 확인됨**. 코드도 apply=True 명시 없인
      쓰기 없음 + CLI가 --target-dir 없는 --apply를 구조적으로 차단하는 걸 직접 확인.
      gap: Cursor/Windsurf/Cline/VSCode 훅 스키마는 정확한 API 미확정이라 TODO 골격.

## 상태 로그
- 2026-07-23: 빌드 시작. 기존 파이프라인 코드 규모 확인 완료(server/·securedoc_mcp/·extension/ 존재).
  securedoc-gateway는 아직 git repo 아님.
- 2026-07-23: U1 완료·검증(pytest 31 통과, 서버 기동+마스킹 반환 실측).
- 2026-07-23: U2 완료·검증(pytest 33 통과, MCP tools/list+scan_text HTTP·stdio 검증).
- 2026-07-23: U3 완료·검증(manifest valid, JS 문법+회귀테스트 통과). 하드코딩 토큰 보안조치 완료.
- 2026-07-23: U4 완료·검증(npm install 성공, 문법검사 19/19, 엔진 사이드카 스모크 실제 spawn+포트탐지 성공).
- 2026-07-23: U5 완료·검증(pytest 40 통과). pyhwpx=한컴오피스 의존 발견 → PLAN.md §11 정정.
- 2026-07-23: U6 완료·검증(pytest 74 통과). 실제 ~/.claude/settings.json 미접촉 직접 확인.
- 2026-07-23: 사용자 요청으로 실브라우저(Playwright+실제 Chrome)에 익스텐션 로드해 실물 테스트 —
  6개 사이트 정찰(크래시 없음) + sidePanel 제스처 체인 격리 검증. **PLAN §변경1의 "content가 직접
  호출" 전제가 틀렸음을 실측으로 발견**(chrome.sidePanel이 content script엔 없음) → PLAN.md
  §변경1/리스크 섹션 정정 + `extension/content/content.js`·`background/service-worker.js` 코드 정정
  (SW 단일 홉 위임을 정식 경로로) 완료. 문법·회귀테스트 재확인 통과.
- **🎉 전체 6개 유닛(U1~U6) 완료 + 실브라우저 검증으로 §변경1 설계 오류 1건 발견·수정.**
  남은 건 6개 실사이트 통합 테스트/앱 GUI 렌더링/패키징 빌드(사람 손 또는 추가 세션 필요) 뿐.

## 재개 절차 (컨텍스트 리셋 후 나에게)
1. 이 파일 읽기 → 첫 번째 `[ ]` 유닛이 현재 목표.
2. 해당 유닛 산출물이 디스크에 이미 있는지 확인(부분 완료 가능성).
3. 서브에이전트로 이어서 구현 → 완료 시 이 파일 `[x]` + 상태 로그 갱신.
4. 모든 유닛 `[x]`면 최종 점검(엔진 기동 테스트) 후 완료 보고.
