# MCP 위험 검사기 (실험용)

검사기 구현은 Campfire 코어와 분리된 로컬 사이드카다. MCP를 설치하거나 위험 도구를 차단하지 않는다.

## 구현된 기능

- `scan`: `tools/list` JSON, 반복 가능한 `--scope`, 선택한 소스 코드를 검사해 **Risk Score(0~100), 감점 이유, 검사 범위**를 출력한다. Security Score는 `100 - Risk Score`다. 점수는 침해 확률이 아니다.
- `probe`: **이미 실행 중인 숫자형 루프백 주소**(`127.0.0.1`, `[::1]`)의 MCP 도구 정의를 읽는다. 원격 URL 직접 조회는 CLI에서 지원하지 않는다.
- `--save-baseline` / `--baseline`: 공개된 도구 정의 전체의 SHA-256 지문을 저장·비교해 추가·삭제·변경을 알린다. `--baseline-key-file`을 함께 쓰면 별도 키의 HMAC-SHA256으로 baseline 변조도 검증한다.
- `proxy`: downstream stdio 세션 동안 하나의 upstream MCP 세션을 유지한다. 도구 인자와 텍스트 응답을 관찰하되 audit 파일에는 원문이 아닌 시간·도구명·전달 여부·위험 신호만 기록한다. 위험 신호가 있어도 호출과 응답을 그대로 전달한다. **차단 기능이나 OS·네트워크 감시는 아니다.**
- `--llm --consent-cloud`: 비밀 형태를 가린 도구 정의를 Solar Pro 3에 보내 검토 의견을 받는다(`UPSTAGE_API_KEY` 필요). 의견은 점수에 반영하지 않는다.
- `serve`: 검사기 자체를 로컬 stdio MCP 서버로 실행한다. 정적 평가, 확인형 loopback probe, 명시적 동의가 필요한 Solar 검토를 MCP 도구로 제공한다.

## 실행

저장소 루트에서 Python 3.10+와 `mcp>=1.26,<2`, `httpx`, `uvicorn`이 필요하다.

```powershell
python -m experiments.mcp_risk_scanner.cli scan experiments/mcp_risk_scanner/fixtures/benign_tools.json --server-id demo
python -m experiments.mcp_risk_scanner.cli probe http://127.0.0.1:PORT/mcp --confirm-connect
python -m experiments.mcp_risk_scanner.cli serve
python -m unittest discover -s experiments/mcp_risk_scanner/tests -q
```

MCP 클라이언트에는 stdio 서버 명령을 `python`, 인자를
`["-m", "experiments.mcp_risk_scanner.cli", "serve"]`, 작업 디렉터리를 저장소 루트로 등록한다.
노출되는 도구는 `assess_mcp_snapshot`, `probe_loopback_mcp`,
`review_mcp_snapshot_with_solar`다.
기본 `serve`는 정적 평가만 허용한다. loopback 접촉은 시작 인자 `--allow-loopback-probe`와
호출 인자 `confirm_connect=true`가 모두 필요하고, Solar 전송은 시작 인자
`--allow-cloud-llm`과 호출 인자 `consent_cloud=true`가 모두 필요하다.

서명 baseline 예시:

```powershell
python -m experiments.mcp_risk_scanner.cli scan tools.json --server-id demo --save-baseline baseline.json --baseline-key-file baseline.key
python -m experiments.mcp_risk_scanner.cli scan tools-new.json --server-id demo --baseline baseline.json --baseline-key-file baseline.key
```

키 파일은 처음 저장할 때 32바이트 난수로 새로 만들며 기존 파일을 덮어쓰지 않는다. 키를 baseline과 분리해 보호해야 한다. 키 없이 만든 format 1 baseline은 변경 비교만 가능하고 자체 변조를 인증하지 않는다.

## Campfire 선택형 연결

- 앱은 검사기 Python 모듈을 import하지 않고 별도 `serve` 프로세스에 stdio MCP로 연결한다.
- `mcpRiskScannerEnabled`와 `SECUREDOC_MCP_RISK_SCANNER_ENABLED`의 기본값은 `false`다. 꺼져 있으면 프로세스를 만들지 않는다.
- 활성화한 경우 `GET /mcp-risk-scanner/v1/status`, `POST /mcp-risk-scanner/v1/assess`를 제공한다. 앱 경계에서는 정적 snapshot 검사만 노출하며 loopback 접촉과 Solar 전송은 허용하지 않는다.
- 검사기 시작·호출 실패는 `unavailable`로 보고할 뿐 엔진 기동, 대상 MCP 호출, 기존 Campfire `/mcp`를 차단하지 않는다.
- 데이터베이스를 공유하지 않는다. 데스크탑 패키지는 이 폴더의 실행 코드만 `resources/engine/mcp_risk_scanner`에 별도 복사한다. 제거할 때는 엔진 어댑터·라우터·기능 플래그·해당 `extraResources` 항목만 삭제하면 된다.

## 정확한 검사 경계

- 소스 검사는 최대 200개·2MB의 `.py`, `.js`, `.ts`, `.mjs`, `.cjs` 파일에 대한 휴리스틱 패턴 검사다. 의존성 검사는 루트의 `package.json`과 `requirements.txt`에서 설치 스크립트와 버전 고정 여부만 확인하며 CVE를 조회하지 않는다.
- proxy의 인자·응답 검사는 각각 정해진 바이트/필드 예산까지만 수행하고 초과분은 `uninspectable_*`로 표시한다. MCP SDK가 응답을 받은 뒤 검사하므로 proxy 자체가 전송 크기나 메모리를 강제하는 격리 경계는 아니다.
- baseline은 공개된 도구 정의의 변화를 찾는다. 정의를 그대로 둔 백엔드 코드·동작 변경은 찾지 못하며, HMAC을 사용해도 대상 서버의 신원이나 안전성을 인증하지 않는다.
- loopback 초기화와 도구 목록 조회도 대상 코드를 동작시킬 수 있다. `probe`와 MCP의 `probe_loopback_mcp`는 명시적 연결 확인을 요구하고 도구 호출은 하지 않는다.
- Solar 전송 전 정규식 기반으로 알려진 비밀 형태를 가리지만 모든 민감정보를 완벽히 식별한다는 보장은 없다.

**100점과 `low_observed_risk`는 안전 인증이 아니다.** 검사하지 않았거나 제한된 영역은 coverage와 `limited_visibility` 판정에 남는다.
