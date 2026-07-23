# securedoc-gateway

업로드/전송 전 PII(개인정보)와 프롬프트 인젝션을 로컬에서 자동 탐지·마스킹하는
보안 게이트웨이입니다. 세 컴포넌트로 구성됩니다.

- **`engine/`** — 로컬 탐지·마스킹 코어 (Python, FastAPI + MCP 어댑터)
- **`desktop/`** — 데스크탑 앱 (Electron) — 엔진을 사이드카로 기동하고 대시보드/트레이 UI 제공
- **`extension/`** — Chrome 확장 (Manifest V3) — 브라우저에서 AI 서비스 업로드를 가로채 보호

## 요구 사항

- [Node.js](https://nodejs.org) 22 이상 (Electron 35이 내부적으로 요구)
- [Python](https://www.python.org) 3.10 이상
- **Windows 또는 macOS**

  > HWPX 파싱은 `pyhwpx`(한/글 프로그램을 COM으로 자동화)를 쓰기 때문에
  > Windows + 한/글 설치 환경에서만 동작합니다. macOS/Linux나 한/글 미설치
  > Windows에서는 이 파일 형식만 "미지원"으로 우아하게 처리되고, 그 외
  > TXT/PDF/DOCX/XLSX/PPTX 파싱과 PII·인젝션 탐지·마스킹은 동일하게 동작합니다.
  > HWP(구버전)는 LibreOffice가 설치돼 있으면 두 OS 모두 동일하게 지원됩니다.

## 설치 (명령어 한 줄)

git clone 없이, 저장소를 내려받아 엔진(Python)·데스크탑 앱(Electron) 의존성까지
한 번에 설치합니다. 기본 설치 위치는 `~/securedoc-gateway` (환경변수
`SECUREDOC_INSTALL_DIR`로 변경 가능).

**Windows (PowerShell):**

```powershell
irm https://raw.githubusercontent.com/theagares/securedoc-gateway/main/scripts/install.ps1 | iex
```

**macOS / Linux:**

```bash
curl -fsSL https://raw.githubusercontent.com/theagares/securedoc-gateway/main/scripts/install.sh | bash
```

설치가 끝나면 안내되는 대로 실행하면 됩니다:

```bash
cd ~/securedoc-gateway/desktop
npm start
```

앱이 뜨면 엔진 사이드카를 자동으로 spawn합니다. 창을 닫아도 트레이에
상주하며, 트레이 아이콘 클릭으로 다시 열 수 있습니다.

설치형 패키지로 빌드하려면 `desktop/`에서:

```bash
npm run dist:win   # Windows nsis 인스톨러
npm run dist:mac   # macOS dmg (서명되지 않은 로컬 빌드)
```

### 소스로 직접 개발하려면 (git clone)

기여하거나 코드를 직접 고치려면 위 원커맨드 설치 대신 일반적인 clone이 낫습니다.

```bash
git clone https://github.com/theagares/securedoc-gateway.git
cd securedoc-gateway

cd engine
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[test]"   # Windows
.venv/bin/python -m pip install -e ".[test]"            # macOS/Linux

cd ../desktop
npm install
npm start
```

단독으로 엔진만 실행해 확인하려면 `python -m app.main` (venv의 python 사용) —
`/health` 응답에 `{"service":"securedoc-gateway","port":48200}` (또는 다음 사용
가능한 포트)가 뜨면 정상입니다.

### Chrome 확장

1. Chrome 주소창에 `chrome://extensions` 입력
2. 우측 상단 "개발자 모드" 켜기
3. "압축해제된 확장 프로그램을 로드합니다" 클릭 → 이 저장소의 `extension/` 폴더 선택

지원 사이트: chatgpt.com, claude.ai, gemini.google.com, copilot.microsoft.com, grok.com, perplexity.ai

## 구조

```
securedoc-gateway/
├── engine/      # PII/인젝션 탐지·마스킹 코어 + MCP 어댑터
├── desktop/     # Electron 데스크탑 앱
└── extension/   # Chrome MV3 확장
```

자세한 설계 문서는 [`PLAN.md`](./PLAN.md), 빌드 진행 이력은
[`BUILD_PROGRESS.md`](./BUILD_PROGRESS.md)를 참고하세요.
