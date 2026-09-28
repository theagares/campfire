# MCP 위험 검사기 (실험용)

검사기 구현은 Campfire 코어와 분리된 로컬 사이드카다. MCP를 설치하거나 위험 도구를 차단하지 않는다.

## 구현된 기능

- `scan`: `tools/list` JSON, 반복 가능한 `--scope`, 선택한 소스 코드를 검사해 **Risk Score(0~100), 감점 이유, 검사 범위**를 출력한다. Security Score는 `100 - Risk Score`다. 점수는 침해 확률이 아니다.
- 각 파일·분석기별로 `completed`, `partial`, `skipped`, `failed`, `out_of_scope` 상태와 제한 사유를 `analysisCompleteness.records`에 남긴다. 이 원장은 누락 범위를 보여 주지만 숫자 점수에는 직접 가산하지 않는다.
- 도구 정의에서는 숨은 HTML/Markdown 주석, 모델 제어 토큰, 긴 공백, UTF-8 Base64·data URI, Unicode 혼합 문자/동형 문자, 과도한 매개변수 설명, 외부 URL·shell 형태 기본값을 고정 규칙과 예산으로 검사한다.
- 소스가 제공되면 선언 scope와 정적으로 추론한 파일·네트워크·환경·비밀·명령 실행 능력을 대조한다. scope 누락·과소 선언·와일드카드를 표시하며, 과대 선언은 소스와 아티팩트 검사가 완전할 때만 제시한다.
- `probe`: **이미 실행 중인 숫자형 루프백 주소**(`127.0.0.1`, `[::1]`)의 MCP 도구 정의를 읽는다. 원격 URL 직접 조회는 CLI에서 지원하지 않는다.
- `--save-baseline` / `--baseline`: 공개된 도구 정의 전체의 SHA-256 지문을 저장·비교한다. 함께 저장한 제한된 의미 요약으로 도구 추가·삭제, scope/권한 확대, read-only 약화, 열린 입력 스키마, 매개변수·위험 기본값·instruction 신호 추가를 구분한다. 요약 예산이 끝나도 전체 SHA-256 비교는 유지된다. `--baseline-key-file`을 함께 쓰면 별도 키의 HMAC-SHA256으로 baseline 변조도 검증한다.
- `proxy`: downstream stdio 세션 동안 하나의 upstream MCP 세션을 유지한다. 도구 인자와 텍스트 응답을 관찰하되 audit 파일에는 원문이 아닌 시간·대상 ID·도구명·전달 여부·위험 신호와 HMAC 무결성 정보만 기록한다. 위험 신호가 있어도 호출과 응답을 그대로 전달한다. **차단 기능이나 OS·네트워크 감시는 아니다.**
- `--llm --consent-cloud`: 비밀 형태를 가린 도구 정의를 Solar Pro 3에 보내 검토 의견을 받는다(`UPSTAGE_API_KEY` 필요). 의견은 점수에 반영하지 않는다.
- `serve`: 검사기 자체를 로컬 stdio MCP 서버로 실행한다. 정적 평가, 확인형 loopback probe, 명시적 동의가 필요한 Solar 검토를 MCP 도구로 제공한다.

## 실행

저장소 루트에서 Python 3.10+와 `mcp>=1.26,<2`, `httpx`, `uvicorn`이 필요하다.

```powershell
python -m experiments.mcp_risk_scanner.cli scan experiments/mcp_risk_scanner/fixtures/benign_tools.json --server-id demo
python -m experiments.mcp_risk_scanner.cli probe http://127.0.0.1:PORT/mcp --confirm-connect
python -m experiments.mcp_risk_scanner.cli proxy http://127.0.0.1:PORT/mcp --audit-file audit.jsonl --audit-key-file audit.key --confirm-connect
python -m experiments.mcp_risk_scanner.cli scan experiments/mcp_risk_scanner/fixtures/benign_tools.json --server-id http://127.0.0.1:PORT/mcp --runtime-audit audit.jsonl --runtime-audit-key-file audit.key
python -m experiments.mcp_risk_scanner.cli serve
python -m unittest discover -s experiments/mcp_risk_scanner/tests -q
```

MCP 클라이언트에는 stdio 서버 명령을 `python`, 인자를
`["-m", "experiments.mcp_risk_scanner.cli", "serve"]`, 작업 디렉터리를 저장소 루트로 등록한다.
노출되는 도구는 `assess_mcp_snapshot`, `create_mcp_baseline`, `probe_loopback_mcp`,
`review_mcp_snapshot_with_solar`다.
기본 `serve`는 정적 평가만 허용한다. loopback 접촉은 시작 인자 `--allow-loopback-probe`와
호출 인자 `confirm_connect=true`가 모두 필요하고, Solar 전송은 시작 인자
`--allow-cloud-llm`과 호출 인자 `consent_cloud=true`가 모두 필요하다.
MCP 도구에서 소스 검사를 허용하려면 시작할 때 `--allow-source-root PATH`를 지정한다.
`source_path`는 그 루트 자체 또는 하위 경로로만 해석되며, 지정하지 않으면 소스 검사가 비활성화된다.
서명 baseline 검증 키도 도구 인자로 받지 않고 시작 시 `--baseline-key-file PATH`로만 설정한다.

서명 baseline 예시:

```powershell
python -m experiments.mcp_risk_scanner.cli scan tools.json --server-id demo --save-baseline baseline.json --baseline-key-file baseline.key
python -m experiments.mcp_risk_scanner.cli scan tools-new.json --server-id demo --baseline baseline.json --baseline-key-file baseline.key
```

키 파일은 처음 저장할 때 32바이트 난수로 새로 만들며 기존 파일을 덮어쓰지 않는다. 키를 baseline과 분리해 보호해야 한다. 키 없이 만든 format 1 baseline은 변경 비교만 가능하고 자체 변조를 인증하지 않는다. runtime audit은 `--audit-file`과 별도의 `--audit-key-file`을 함께 지정해야 하며, 서명되지 않은 caller 제공 이벤트는 점수 근거가 될 수 있어도 `checked_mcp_messages` coverage로 신뢰하지 않는다.

## Campfire 선택형 연결

- 앱은 검사기 Python 모듈을 import하지 않고 별도 `serve` 프로세스에 stdio MCP로 연결한다.
- `mcpRiskScannerEnabled`와 `SECUREDOC_MCP_RISK_SCANNER_ENABLED`의 기본값은 `false`다. 꺼져 있으면 프로세스를 만들지 않는다.
- 활성화한 경우 `GET /mcp-risk-scanner/v1/status`, `POST /mcp-risk-scanner/v1/assess`를 제공한다. 앱 경계에서는 정적 snapshot 검사만 노출하며 로컬 소스 경로, caller 제공 runtime 이벤트, loopback 접촉, Solar 전송은 허용하지 않는다.
- 검사기 시작·호출 실패는 `unavailable`로 보고할 뿐 엔진 기동, 대상 MCP 호출, 기존 Campfire `/mcp`를 차단하지 않는다.
- 데이터베이스를 공유하지 않는다. 데스크탑 패키지는 이 폴더의 실행 코드만 `resources/engine/mcp_risk_scanner`에 별도 복사한다. 제거할 때는 엔진 어댑터·라우터·기능 플래그·해당 `extraResources` 항목만 삭제하면 된다.

## 정확한 검사 경계

- 소스/설정 검사는 최대 5,000개 파일시스템 항목에서 최대 200개·2MB만 읽는다. `.py`, `.js`, `.ts`, `.mjs`, `.cjs`, `.sh`, `.ps1`, `.bat`, `.cmd`에는 휴리스틱 패턴을 적용하고 JSON/TOML/YAML 설정에서는 MCP 실행 명령의 버전 고정을 확인한다. `npx`, `uvx`, `pip install`, Docker 참조 검사는 문자열 휴리스틱이며 패키지의 실제 해시나 배포자 신원은 확인하지 않는다.
- Python은 실행하지 않고 AST를 파일당 최대 50,000노드까지 파싱하고 taint 표현식은 최대 200,000노드 방문까지만 추적한다. `exec`/`eval`/동적 import/명령 실행/위험한 역직렬화와 제한된 이름 기반 taint 흐름을 찾는다. 이 분석은 경로·별칭·프레임워크를 완전히 모델링하지 않으므로 오탐과 누락이 가능하다. 파싱 실패나 예산 초과는 `partial`/`skipped`로 남는다.
- 소스 상세 신호는 500개, 전체 상세 finding은 1,000개까지만 보존한다. 초과분은 category별 합계로 계속 점수와 critical 판정에 반영하되 상세 evidence가 생략됐음을 coverage와 검사 원장에 표시한다.
- 의존성 검사는 루트의 `package.json`과 `requirements.txt`에서 설치 스크립트와 버전 고정 여부만 확인하며 CVE를 조회하지 않는다. `.pyc`, 실행 바이너리, 압축 파일, 지원하지 않는 실행 스크립트, symlink/junction은 실행·확장·추적하지 않고 아티팩트 원장과 제한된 coverage로 표시한다. 확장자가 없는 파일의 binary 판별도 최대 256KB의 고정 peek 예산 안에서만 수행한다.
- proxy의 인자·응답 검사는 각각 정해진 바이트/필드 예산까지만 수행하고 초과분은 `uninspectable_*`로 표시한다. MCP SDK가 응답을 받은 뒤 검사하므로 proxy 자체가 전송 크기나 메모리를 강제하는 격리 경계는 아니다. 대상 MCP 작업에는 기본 제한시간을 추가하지 않으며, 운영자가 `--call-timeout SECONDS`를 지정하면 그 제한 때문에 원래 동작과 달라질 수 있다.
- baseline은 공개된 도구 정의와 호출 시 제공된 scope의 변화를 찾는다. 정의를 그대로 둔 백엔드 코드·동작 변경은 찾지 못하며, 의미 요약이 예산 때문에 생략된 도구는 일반 `modified`로만 표시된다. HMAC을 사용해도 대상 서버의 신원이나 안전성을 인증하지 않는다.
- loopback 초기화와 도구 목록 조회도 대상 코드를 동작시킬 수 있다. `probe`와 MCP의 `probe_loopback_mcp`는 명시적 연결 확인을 요구하고 도구 호출은 하지 않는다.
- Solar 전송 전 정규식 기반으로 알려진 비밀 형태를 가리지만 모든 민감정보를 완벽히 식별한다는 보장은 없다.

**100점과 `low_observed_risk`는 안전 인증이 아니다.** 검사하지 않았거나 제한된 영역은 coverage와 `limited_visibility` 판정에 남는다.
