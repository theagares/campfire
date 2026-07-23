# securedoc-gateway

업로드/전송 전 PII(개인정보)와 프롬프트 인젝션을 로컬에서 자동 탐지·마스킹하는
보안 게이트웨이입니다. 세 컴포넌트로 구성됩니다.

- **`engine/`** — 로컬 탐지·마스킹 코어 (Python, FastAPI + MCP 어댑터)
- **`desktop/`** — 데스크탑 앱 (Electron) — 엔진을 사이드카로 기동하고 대시보드/트레이 UI 제공
- **`extension/`** — Chrome 확장 (Manifest V3) — 브라우저에서 AI 서비스 업로드를 가로채 보호

## 요구 사항

- [Node.js](https://nodejs.org) 22 이상 (Electron 35이 내부적으로 요구)
- [Python](https://www.python.org) 3.10 이상
- **Windows** (엔진 의존성 `pyhwpx`가 HWP 파싱을 위해 `pywin32`/COM 자동화를 사용하므로 현재는 Windows에서만 설치가 됩니다. macOS/Linux 지원은 추후 과제)

## 설치 및 실행

### 1) 저장소 클론

```bash
git clone https://github.com/theagares/securedoc-gateway.git
cd securedoc-gateway
```

### 2) 엔진 (Python)

데스크탑 앱이 개발 모드에서 이 폴더를 사이드카로 그대로 실행하므로,
먼저 가상환경을 만들고 의존성을 설치해야 합니다.

```bash
cd engine
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[test]"
```

단독 실행 확인 (선택):

```bash
.venv\Scripts\python.exe -m app.main
```

`/health` 응답에 `{"service":"securedoc-gateway","port":48200}` (또는 그 다음 사용 가능한 포트)가 뜨면 정상입니다.

### 3) 데스크탑 앱 (Electron)

```bash
cd ../desktop
npm install
npm start
```

앱이 뜨면 엔진 사이드카(위 2번 venv)를 자동으로 spawn합니다. 창을 닫아도
트레이에 상주하며, 트레이 아이콘 클릭으로 다시 열 수 있습니다.

설치형 패키지(exe/dmg)로 빌드하려면:

```bash
npm run dist:win   # Windows nsis 인스톨러
```

### 4) Chrome 확장

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
