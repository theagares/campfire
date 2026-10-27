"""
app/config.py
엔진 설정 중앙 관리 (PLAN §11 포트, §9.2 타임아웃, §5 detector 선택).

모든 모듈은 이 파일에서만 설정값을 읽는다. 환경변수로 재정의 가능.
"""

import os
import sys
from pathlib import Path

APP_DIR: Path = Path(__file__).resolve().parent

# ── 모델 보관 위치 ────────────────────────────────────────────────────────────
# 내려받은 가중치는 **앱 설치 폴더 밖**에 둔다. 설치 폴더(prod 기준
# resources/engine) 는 재설치·업데이트 때 통째로 교체되므로, 거기 두면 앱을 다시
# 받을 때마다 이미 갖고 있는 600MB 를 또 받게 된다(실사용자 리포트).
#
# 앱과 함께 배포되는 건 런타임 스크립트·설정 같은 작은 파일들뿐이고(BUNDLED_MODELS_DIR),
# 엔진 기동 시 그것들을 이 보관 위치로 복사해 맞춘다(models_sync.py). 그래서 이 두
# 디렉터리는 "앱이 주는 코드" 와 "사용자 기기에 쌓이는 가중치" 를 나누는 경계다.
BUNDLED_MODELS_DIR: Path = APP_DIR / "models"


def _models_base() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local"))
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    return Path(os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share"))


# UpSecurity -> Campfire 리브랜딩 이전에 쓰던 보관 위치. 여기에 이미 받아둔 가중치는
# 약 600MB 라, 새 경로만 보고 "없다" 고 판단하면 기존 사용자가 전부 다시 받게 된다.
# 새 경로가 비어 있고 옛 경로에 내용이 있으면 그대로 옮겨 쓴다(_migrate_legacy_root).
LEGACY_MODELS_ROOT: Path = _models_base() / "UpSecurity" / "models"


def _default_models_root() -> Path:
    return _models_base() / "Campfire" / "models"


MODELS_ROOT: Path = Path(os.environ.get("SECUREDOC_MODELS_DIR", str(_default_models_root())))

# ── 서비스 시그니처 (PLAN §11) ────────────────────────────────────────────────
# 익스텐션이 포트 스캔 시 "우리 엔진"임을 식별하는 고정 시그니처.
SERVICE_NAME: str = "campfire"

# ── 포트 자동 스캔 (PLAN §11) ─────────────────────────────────────────────────
BASE_PORT: int = int(os.environ.get("SECUREDOC_BASE_PORT", "48200"))
PORT_SCAN_COUNT: int = int(os.environ.get("SECUREDOC_PORT_SCAN_COUNT", "10"))  # 48200~48209
HOST: str = os.environ.get("SECUREDOC_HOST", "127.0.0.1")

# The packaged desktop app owns user-defined mandatory mask terms.  It sends
# the decrypted rules over an authenticated localhost-only control endpoint
# after every engine start; scans fail closed until that first sync arrives.
DESKTOP_MANAGED: bool = os.environ.get("SECUREDOC_DESKTOP_MANAGED", "0").strip().lower() in {
    "1", "true", "yes", "on"
}
# 읽고 바로 환경에서 지운다 — 남겨 두면 엔진이 띄우는 검출기 자식 프로세스(**os.environ)에
# 그대로 상속된다. 엔진은 uvicorn 을 같은 프로세스에서 돌리므로 다시 읽을 일이 없다.
INTERNAL_CONTROL_TOKEN: str = os.environ.pop("SECUREDOC_INTERNAL_CONTROL_TOKEN", "")

# 실제 바인딩된 포트. main.py 가 기동 시점에 채운다. /health 가 이 값을 반환.
BOUND_PORT: int | None = None

# ── 선택형 MCP 위험 검사기 사이드카 ──────────────────────────────────────────
#
# 앱 코어와 검사기 구현을 분리한다. 엔진은 아래 실행 정보로 **별도 stdio MCP
# 프로세스**를 띄우며, 검사기 Python 모듈을 import하지 않는다. 기본값은 꺼짐이다.
# 데스크탑 배포본은 experiments/mcp_risk_scanner 를 resources/engine/mcp_risk_scanner
# 로 별도 복사하므로, 나중에 이 설정·어댑터·패키징 항목만 제거해도 앱 코어에는 영향이
# 없다. 개발 체크아웃에서는 원래 experiments 패키지를 그대로 사용한다.
MCP_RISK_SCANNER_ENABLED: bool = os.environ.get(
    "SECUREDOC_MCP_RISK_SCANNER_ENABLED", "0"
).strip().lower() in {"1", "true", "yes", "on"}
MCP_RISK_SCANNER_PYTHON_EXECUTABLE: str = os.environ.get(
    "SECUREDOC_MCP_RISK_SCANNER_PYTHON_EXECUTABLE", sys.executable
)

_bundled_scanner = APP_DIR.parent / "mcp_risk_scanner"
_checkout_root = APP_DIR.parent.parent
if _bundled_scanner.is_dir():
    _default_scanner_module = "mcp_risk_scanner.cli"
    _default_scanner_pythonpath = APP_DIR.parent
else:
    _default_scanner_module = "experiments.mcp_risk_scanner.cli"
    _default_scanner_pythonpath = _checkout_root

MCP_RISK_SCANNER_MODULE: str = os.environ.get(
    "SECUREDOC_MCP_RISK_SCANNER_MODULE", _default_scanner_module
)
MCP_RISK_SCANNER_PYTHONPATH: Path = Path(os.environ.get(
    "SECUREDOC_MCP_RISK_SCANNER_PYTHONPATH", str(_default_scanner_pythonpath)
))
MCP_RISK_SCANNER_START_TIMEOUT_SEC: float = float(os.environ.get(
    "SECUREDOC_MCP_RISK_SCANNER_START_TIMEOUT_SEC", "10"
))
MCP_RISK_SCANNER_CALL_TIMEOUT_SEC: float = float(os.environ.get(
    "SECUREDOC_MCP_RISK_SCANNER_CALL_TIMEOUT_SEC", "15"
))

# ── 파이프라인 (PLAN §6) ──────────────────────────────────────────────────────
# 레거시/공용 문자 청크 기본값. PII는 원문 전체를 detector에 넘긴 뒤 아래
# PII_WINDOW_SIZE/OVERLAP으로 한 번만 자르고, 인젝션은 별도 토큰 청크를 사용한다.
CHUNK_SIZE: int = int(os.environ.get("SECUREDOC_CHUNK_SIZE", "1000"))

# 인젝션 탐지용 슬라이딩 윈도우. 실제 EXAONE 토크나이저 기준이라 한글/영문 비율이
# 달라도 모델이 보는 입력 크기가 일정하다. 48토큰 겹침은 이번 100페이지 회귀 문서의
# 한국어 전용 19토큰 회귀 인젝션이 어떤 경계에 놓여도 적어도 한 윈도우에 온전히
# 들어갈 여유를 남기면서
# 중복 추론량을 줄인다.
INJECTION_CHUNK_TOKENS: int = max(
    2, int(os.environ.get("SECUREDOC_INJECTION_CHUNK_TOKENS", "128"))
)
INJECTION_CHUNK_OVERLAP_TOKENS: int = max(
    0,
    min(
        INJECTION_CHUNK_TOKENS - 1,
        int(os.environ.get("SECUREDOC_INJECTION_CHUNK_OVERLAP_TOKENS", "48")),
    ),
)

# ── 요청 제한 ─────────────────────────────────────────────────────────────────
MAX_UPLOAD_BYTES: int = int(os.environ.get("SECUREDOC_MAX_UPLOAD_BYTES", str(20 * 1024 * 1024)))
MAX_PROMPT_CHARS: int = int(os.environ.get("SECUREDOC_MAX_PROMPT_CHARS", "100000"))

# 파싱 결과 텍스트의 상한(자).
#
# 업로드는 MAX_UPLOAD_BYTES 로 막지만 "추출된 텍스트" 길이는 아무도 안 봤다. 청크는
# CHUNK_SIZE(1,000) / step 900 이라 텍스트가 길어지면 청크 수가 그대로 따라 늘고,
# 청크마다 PII·인젝션 추론이 붙는다 — 20MB 짜리가 텍스트로 풀리면 청크 2만 개가 넘는다.
# 값의 근거: 이 파일 아래 DETECT_CONCURRENCY 주석의 실측치(10만 자 = 111청크,
# 15만 자 = 167청크)를 기준으로, 그 배쯤에서 끊는다(20만 자 ≈ 222청크).
# 넘으면 앞부분만 검사하고 결과에 truncated 로 알린다(조용히 자르지 않는다).
MAX_TEXT_CHARS: int = int(os.environ.get("SECUREDOC_MAX_TEXT_CHARS", "200000"))

# ── 타임아웃 (PLAN §9.2) ──────────────────────────────────────────────────────
# 파싱 한 건의 상한(초).
#
# 예전 이름은 REQUEST_TIMEOUT_SEC 였는데 코드 어디에서도 쓰이지 않는 죽은 값이었다.
# "타임아웃이 있다" 고 보이지만 실제로는 없었고, parser 의 STATUS_TIMEOUT 도 만드는
# 쪽이 없어 같이 죽어 있었다. 둘을 실제로 잇는다 — 이 상한을 넘긴 파싱은 STATUS_TIMEOUT
# 으로 "미검사 통과" 처리되고 사용자에게 고지된다.
PARSE_TIMEOUT_SEC: float = float(os.environ.get("SECUREDOC_PARSE_TIMEOUT_SEC", "30"))

# 파서는 스레드 타임아웃으로 중단할 수 없으므로 별도 프로세스에서 실행한다. 동시에
# 처리할 워커와 대기 요청을 모두 제한해 느리거나 손상된 문서가 CPU/RAM을 무한히
# 점유하지 않게 한다. 워커는 첫 요청 때만 뜨며 타임아웃 뒤 폐기·재생성된다.
PARSE_WORKERS: int = max(1, int(os.environ.get("SECUREDOC_PARSE_WORKERS", "2")))
PARSE_QUEUE_SIZE: int = max(0, int(os.environ.get("SECUREDOC_PARSE_QUEUE_SIZE", "2")))
PARSE_WORKER_START_TIMEOUT_SEC: float = max(
    1.0, float(os.environ.get("SECUREDOC_PARSE_WORKER_START_TIMEOUT_SEC", "10"))
)

# 검출기 서브프로세스에 요청을 써넣고 응답 한 줄을 기다리는 상한(초).
# 없으면 서브프로세스가 멎었을 때 readline() 이 영원히 대기하는데, 그 대기가
# _request_lock 안에서 일어나므로 뒤에 줄 선 모든 청크까지 같이 멎는다
# (50장 문서 = 청크 111개가 통째로 잠긴다). 죽은 것과 도는 중인 것을 구분할 수
# 없게 되는 것도 문제라 상한을 둔다.
# 값의 근거: 이건 "한 청크의 추론 시간" 상한이지 문서 전체가 아니다(락 대기 시간은
# 포함되지 않는다 — 요청을 쓴 뒤부터 재니까). GPU 에서는 청크당 0.1초 안팎이지만
# CPU 폴백(PII_DEVICE/INJECTION_DEVICE=cpu)에서는 훨씬 느려서 넉넉히 잡는다.
DETECT_INFER_TIMEOUT_SEC: float = float(
    os.environ.get("SECUREDOC_DETECT_INFER_TIMEOUT_SEC", "120")
)

# 청크별 detect() 를 한 번에 몇 개까지 띄울지.
# 예전엔 상한 없이 asyncio.gather 로 전부 띄웠다. 청크가 2~3개인 문서를 전제로 한
# 최적화였는데(Solar API 대기를 겹치게 해서 총 대기시간을 줄이는 목적), 문서가 길면
# 그 팬아웃이 그대로 커진다 — 실측: 50장(10만 자) = 청크 111개가 동시에 진입,
# 15만 자면 167개. 모델 추론 자체는 각 detector 의 _request_lock 이 직렬화하므로
# GPU 가 동시에 물리진 않지만, 락 밖의 Solar API 호출이 그 수만큼 한꺼번에 나가고
# 태스크/대기 자원도 그만큼 쌓인다. 작은 문서의 이득은 유지하면서 상한만 둔다.
DETECT_CONCURRENCY: int = max(
    1, int(os.environ.get("SECUREDOC_DETECT_CONCURRENCY", "8"))
)

# ── Detector 선택 (PLAN §5 registry) ─────────────────────────────────────────
# 룰베이스 폴백은 완전히 제거했다 — encoder/llm_mcp(실 모델)만 남는다. 가중치가
# 아직 안 받아진 상태에서는 detector 자체가 아니라 파이프라인의 model_status 게이트가
# 미검사 통과를 처리한다(app/core/pipeline/orchestrator.py).

# ── 인젝션 정책 (PLAN §4 / §8) ────────────────────────────────────────────────
# mask: 구간을 [인젝션 마스킹]으로 치환 후 통과 (기본). block: 인젝션 탐지 시 차단.
INJECTION_POLICY: str = os.environ.get("SECUREDOC_INJECTION_POLICY", "mask")

# ── GPU 상주 정책 (PLAN §4.1, Phase 6) ───────────────────────────────────────
# PII 인코더: 항시 상주(always_on) — 크기가 작아 모든 요청 1차 검사에 지연 없이
#   즉답해야 하므로 앱 기동 시 즉시 "로드 완료" 상태로 취급한다(v1 룰베이스/encoder
#   스텁 둘 다 실제 GPU 메모리는 쓰지 않으므로 상태 플래그로만 흉내).
# 인젝션 LLM: 유휴 언로드(idle_unload) — 마지막 사용 후 아래 idle timeout 이 지나면
#   "언로드" 상태로 전환. 언로드 상태에서 요청이 오면 검사를 생략하지 않고
#   fail-closed 로 아래 load delay 만큼 "로드 완료"까지 대기한 뒤 검사한다.
INJECTION_LLM_IDLE_TIMEOUT_SEC: float = float(
    os.environ.get("SECUREDOC_INJECTION_LLM_IDLE_TIMEOUT_SEC", str(10 * 60))
)  # 기본 10분 (PLAN §4.1)
INJECTION_LLM_LOAD_DELAY_SEC: float = float(
    os.environ.get("SECUREDOC_INJECTION_LLM_LOAD_DELAY_SEC", "180")
)  # 실제 모델(서브프로세스) 로딩 완료를 기다리는 최대 시간(초). 콜드 스타트 시
   # EXAONE-3.5-2.4B-Instruct 로드에 수십 초가 걸릴 수 있어 넉넉히 잡는다.

# ── 인젝션 LLM 실제 모델 (EXAONE-4.0-1.2B + hybrid(attention token-pair + segment
#   hidden-state) regularized MLP, ho님의 injection_diag 프로젝트 model_release
#   exaone-4.0-1.2b_hybrid_segment_v1 번들을 로컬 서브프로세스 sidecar 로 실행).
#   실측 표준화 통계(norm_stats.pt)가 제대로 저장돼 있어(이전 EXAONE-3.5-2.4B
#   번들은 identity fallback 이라 사실상 무용했음), 실제로 입력을 구분해 판정한다.
#   pooled 8도메인 기준: hybrid Acc 99.2%/FPR 0.25%/FNR 0.66%,
#   attn(hidden 미사용) Acc 97.9%/FPR 1.69%/FNR 0.56% — hybrid 권장(기본값).
INJECTION_ENGINE_DIR: Path = MODELS_ROOT / "injection_engine"
INJECTION_VARIANT: str = os.environ.get("SECUREDOC_INJECTION_VARIANT", "hybrid")  # "hybrid" | "attn"
# 기본값 "auto" — local_injection_hybrid_inference.py 의 _resolve_device() 가
# torch.cuda.is_available()/mps 를 실제로 확인해 cuda/mps/cpu 를 고른다. 예전엔
# "cuda" 로 하드코딩돼 있어, GPU가 있어도 배포판 torch 가 CPU 전용 빌드면(CI에서
# 실측 확인됨: pip install torch 기본이 CPU 휠) AssertionError("Torch not compiled
# with CUDA enabled")로 무조건 죽었다 — "auto" 는 그 경우에도 안전하게 CPU 로 폴백한다.
INJECTION_DEVICE: str = os.environ.get("SECUREDOC_INJECTION_DEVICE", "auto")
INJECTION_DTYPE: str = os.environ.get("SECUREDOC_INJECTION_DTYPE", "bfloat16")
INJECTION_MAX_SEQ_LEN: int = int(os.environ.get("SECUREDOC_INJECTION_MAX_SEQ_LEN", "4096"))
# 128-token document windows benchmarked best in groups of eight.  Standard
# prompts render to at most 192 model tokens, so that is the reusable CUDA shape;
# unusually long user prompts automatically move to a larger 32-token bucket.
INJECTION_BATCH_SIZE: int = max(
    1, int(os.environ.get("SECUREDOC_INJECTION_BATCH_SIZE", "8"))
)
# CPU·MPS 에서는 요청 하나에 이만큼만 담는다. 8개 묶음은 CUDA 기준이라, 느린 CPU(CI 의 Intel
# mac·Windows 러너)에서는 요청 하나가 DETECT_INFER_TIMEOUT_SEC(120초)를 넘겨 프로세스가 죽었다.
# 2개면 요청 하나의 계산량이 예전 1,000자 청크 하나와 비슷하다.
INJECTION_CPU_BATCH_SIZE: int = max(
    1, int(os.environ.get("SECUREDOC_INJECTION_CPU_BATCH_SIZE", "2"))
)
INJECTION_PAD_TOKENS: int = max(
    1,
    min(
        INJECTION_MAX_SEQ_LEN,
        int(os.environ.get("SECUREDOC_INJECTION_PAD_TOKENS", "192")),
    ),
)
# Eager attention remains mandatory because the trained classifier consumes
# attention probabilities.  Compact capture retains only its 1,024 sampled
# positions per layer instead of all layer matrices.
INJECTION_COMPACT_ATTENTIONS: bool = os.environ.get(
    "SECUREDOC_INJECTION_COMPACT_ATTENTIONS", "1"
).strip().lower() in {"1", "true", "yes", "on"}
# The hybrid head only consumes pooled values from layers 8/15/22/30.  Capture
# those pools in-place instead of retaining every token from every hidden layer.
INJECTION_COMPACT_HIDDEN_STATES: bool = os.environ.get(
    "SECUREDOC_INJECTION_COMPACT_HIDDEN_STATES", "1"
).strip().lower() in {"1", "true", "yes", "on"}
# Windows CUDA has no supported Triton runtime, and the cudagraphs backend added
# 8.7s of warm-up without improving steady-state latency in the 100-page benchmark.
# Keep compilation available for explicit platform validation, but use the measured
# eager path by default.
INJECTION_COMPILE: bool = os.environ.get(
    "SECUREDOC_INJECTION_COMPILE", "0"
).strip().lower() in {"1", "true", "yes", "on"}
INJECTION_PYTHON_EXECUTABLE: str = os.environ.get("SECUREDOC_INJECTION_PYTHON_EXECUTABLE", sys.executable)
# gateway 파이프라인은 문서 청크 텍스트 한 덩어리만 주지만, 이 분류기는 원래
# system_prompt/user_prompt/tool_response 3필드(간접 인젝션: 에이전트가 도구 응답을
# 받는 상황)를 전제로 학습됐다. 청크 텍스트를 tool_response 에 넣고, 아래 두
# placeholder 로 "문서 검토/요약"이라는 gateway 실사용 맥락을 근사한다.
INJECTION_LLM_SYSTEM_PROMPT: str = os.environ.get(
    "SECUREDOC_INJECTION_LLM_SYSTEM_PROMPT", "당신은 문서를 검토하고 요약하는 보조 AI입니다."
)
INJECTION_LLM_USER_PROMPT: str = os.environ.get(
    "SECUREDOC_INJECTION_LLM_USER_PROMPT", "아래 문서 내용을 검토하고 핵심을 요약해 주세요."
)


def _load_dotenv_value(key: str) -> str:
    """프로젝트 루트 .env(엔진 전용이 아니라 여러 컴포넌트가 공유하는 파일)에서
    key=value 한 줄을 읽는다. python-dotenv 등 별도 의존성 없이 최소한만 지원."""
    dotenv_path = APP_DIR.parent.parent / ".env"  # engine/app -> engine -> 레포 루트/.env
    if not dotenv_path.exists():
        return ""
    for line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == key:
            return v.strip()
    return ""


# ── 인젝션 2단계 세부 위치 특정 (Upstage Solar Pro 4) ─────────────────────────
# EXAONE hybrid 분류기는 청크 전체를 misaligned/aligned/non_instruction 로만
# 판정하고 청크 내 구체적 위치는 모르는 구조라(어텐션/hidden state 를 청크 전체에
# 걸쳐 풀링해서 분류), misaligned 판정 시 청크 전체가 통째로 마스킹된다. Solar
# Pro 4 에 "이 청크에서 실제 인젝션 지시문이 정확히 어느 부분이냐"를 다시 물어
# 그 부분만 정밀하게 마스킹하기 위한 2단계 호출. 1단계(EXAONE)가 이미
# misaligned 라고 판정한 청크에 대해서만 호출하므로 비용/지연이 항상 붙지 않는다.
# 실패/애매하면(빈 응답, API 오류, 응답이 원문과 정확히 일치하지 않음) 기존처럼
# 청크 전체를 마스킹하는 fail-safe 로 되돌아간다(검사 자체를 생략하지 않음).
# 앱 설정 화면 키(SECUREDOC_)를 먼저 본다. 데스크탑은 PC 환경변수를 엔진에 물려주므로,
# UPSTAGE_API_KEY 를 먼저 보면 다른 도구용으로 PC 에 둔 키가 설정 화면 키를 덮는다.
UPSTAGE_API_KEY: str = (
    os.environ.get("SECUREDOC_UPSTAGE_API_KEY")
    or os.environ.get("UPSTAGE_API_KEY")
    or _load_dotenv_value("upstage_key")
)
UPSTAGE_API_BASE: str = os.environ.get(
    "SECUREDOC_UPSTAGE_API_BASE", "https://api.upstage.ai/v1/chat/completions"
)
UPSTAGE_MODEL: str = os.environ.get("SECUREDOC_UPSTAGE_MODEL", "solar-pro4")
UPSTAGE_TIMEOUT_SEC: float = float(os.environ.get("SECUREDOC_UPSTAGE_TIMEOUT_SEC", "20"))
# API 키가 없으면(로컬 전용 배포 등) 자동으로 비활성화 — 2단계 없이 기존 청크
# 전체 마스킹 동작 그대로 유지.
INJECTION_LOCALIZE_ENABLED: bool = bool(UPSTAGE_API_KEY)

# ── 기밀 탐지 (회사 KG + 리트리버 + Solar Pro 4) ─────────────────────────────
# 문서에서 KG 엔티티 이름을 찾아(사전 매칭) 주어·목적어가 둘 다 나온 트리플만 리트리버가
# 채점하고, 문서가 말하는 것으로 보이는 트리플 전부와 PII 를 가린 문서 전체를 Solar 가 한 번에
# "어느 문장이 진술했고 기밀인가" 판단한다. 문서가 외부(Solar)로 나가므로 데스크톱 설정의 동의 토글로만 켠다.
CONFIDENTIAL_ENABLED: bool = os.environ.get("SECUREDOC_CONFIDENTIAL_ENABLED", "0") == "1"
KG_PATH: str = os.environ.get("SECUREDOC_KG_PATH", "")
CONFIDENTIAL_ENGINE_DIR: Path = MODELS_ROOT / "confidential_engine"
CONFIDENTIAL_DEVICE: str = os.environ.get("SECUREDOC_CONFIDENTIAL_DEVICE", "auto")
CONFIDENTIAL_MAX_LENGTH: int = int(os.environ.get("SECUREDOC_CONFIDENTIAL_MAX_LENGTH", "1024"))  # 토큰 (학습 때와 같음)
# 리트리버가 한 번에 읽는 글자 수. 문장 단위로 묶고, 주어·목적어가 같은 창에 있어야 채점된다.
CONFIDENTIAL_WINDOW_CHARS: int = int(os.environ.get("SECUREDOC_CONFIDENTIAL_WINDOW_CHARS", "1200"))
# 여유값(s − s_TH) 이 이보다 크면 Solar 에게 넘긴다. 정밀도는 Solar 가 맡고 Solar 는 문서당 한 번이라
# 더 넘겨도 호출이 늘지 않으므로 재현율 기준으로 골랐다. 3단계 리트리버(한국어 + 회사 합성 술어 180개) 실측:
# 합성 테스트 처음 보는 술어 재현율 δ −6 0.94 / −10 0.99 (오답 통과 0.05 / 0.19), 헥토 시연 문서 10개의
# 기밀 사실 가림 δ −6 95/105 / −10 104/105 (놓친 것은 길고 빽빽한 문서의 −6 ~ −9 여유값)
CONFIDENTIAL_DELTA: float = float(os.environ.get("SECUREDOC_CONFIDENTIAL_DELTA", "-10.0"))
# 시연 전용 — 기밀 판정 과정(KG 후보·리트리버 점수·Solar 답·가린 문장)을 메모리에만 두고 /demo/dashboard 로 보여 준다.
# 원문 비저장 원칙의 예외라 기본 꺼짐·설정 화면에 없음. 켜면 같은 PC 의 다른 프로그램도 가린 문장을 볼 수 있다 (docs/PRIVACY.md)
DEMO_TRACE_ENABLED: bool = os.environ.get("SECUREDOC_DEMO_TRACE", "0") == "1"

# ── PII 인코더 실제 모델 (A.X-Encoder + Gazetteer + CRF + 문자 경계 MLP + 개체별 MASK/KEEP head,
#   pii_direct_char_boundary_seed42 번들을 로컬 서브프로세스 sidecar 로 실행) ──────────────
# 가중치는 models/<PII_MODEL_SEED> 에 둔다. 이름이 예전(seed42)과 달라야 기존 사용자의 예전
# 가중치를 새 모델로 오인하지 않고 pii_engine_v3 를 받는다.
PII_ENGINE_DIR: Path = MODELS_ROOT / "pii_engine"
PII_MODEL_SEED: str = os.environ.get("SECUREDOC_PII_MODEL_SEED", "direct_char_boundary_seed42")
# GPU 실측(1,500자 청크 기준): CPU 1.69초 vs GPU 0.43초(~4배), VRAM 피크 1.74GB —
# 인젝션 LLM(피크 3.8GB)과 동시 로드해도 합계 ~5.5GB 로 6GB+ VRAM 환경에선 여유 있음.
# 기본값 "auto" — INJECTION_DEVICE와 동일한 이유(위 주석 참고): CPU 전용 torch가
# 배포됐을 때도 죽지 않고 CPU로 폴백하게 한다.
PII_DEVICE: str = os.environ.get("SECUREDOC_PII_DEVICE", "auto")
# 런타임이 FP32 로 고정한다(번들은 FP32 로만 검증됐다). 값은 예전 설정·CLI 호환용으로만 남는다.
PII_DTYPE: str = os.environ.get("SECUREDOC_PII_DTYPE", "float32")
# 직접 400/60 윈도우, CUDA FP16 기준 batch 8보다 p50 10.8% 단축되며
# 피크 VRAM 증가는 34.7MiB였다(대표 회귀 입력 138윈도우, 각 10회 실측).
PII_BATCH_SIZE: int = int(os.environ.get("SECUREDOC_PII_BATCH_SIZE", "16"))
PII_MAX_LENGTH: int = int(os.environ.get("SECUREDOC_PII_MAX_LENGTH", "256"))  # 토큰 기준(모델 권장 기본값)
# 위 PII_MAX_LENGTH(토큰)보다 청크가 길면 뒷부분이 잘리므로, detect() 내부에서
# 문자 기준 슬라이딩 윈도우로 잘라 여러 건을 한 번에 배치 추론한다.
# 실측: 숫자/기호가 조밀한 PII 텍스트(최악의 경우)도 400자가 209토큰이라
# 256토큰 한도에 여유(~18%)가 있다 — 200자였을 때보다 청크당 윈도우 수가
# 거의 절반으로 줄어 그만큼 forward pass 횟수가 줄어든다(속도 최적화).
PII_WINDOW_SIZE: int = int(os.environ.get("SECUREDOC_PII_WINDOW_SIZE", "400"))
PII_WINDOW_OVERLAP: int = int(os.environ.get("SECUREDOC_PII_WINDOW_OVERLAP", "60"))
PII_PYTHON_EXECUTABLE: str = os.environ.get("SECUREDOC_PII_PYTHON_EXECUTABLE", sys.executable)
PII_LOAD_TIMEOUT_SEC: float = float(os.environ.get("SECUREDOC_PII_LOAD_TIMEOUT_SEC", "60"))
# PII sidecar 요청 하나에 담는 윈도우 수. 문서 전체를 한 요청으로 보내면 응답 한 줄이
# 끝없이 커지고(긴 문서에서 수 MB) 추론 시간 상한도 문서 전체에 걸린다 — 요청마다 상한을 둔다.
PII_WINDOWS_PER_RPC: int = max(1, int(os.environ.get("SECUREDOC_PII_WINDOWS_PER_RPC", "32")))
# 하위 프로세스 stdout 한 줄(JSONL 응답)의 읽기 상한. asyncio 기본값 64KiB 는 긴 문서의
# 응답 한 줄을 넘지 못해 ValueError 로 검사 전체가 실패했다.
SUBPROCESS_STREAM_LIMIT: int = 2 ** 26

# CUDA에서는 두 모델을 한 프로세스에 상주시켜 명시적인 stream 두 개로 동시에
# 실행한다. CUDA가 없거나 통합 worker 로딩/추론이 실패하면 detector가 유지하던
# 기존 개별 sidecar 경로로 자동 폴백한다. 환경변수로 끄면 처음부터 개별 경로만 쓴다.
# macOS 에는 CUDA 가 없으므로 기본으로 끈다(앱 시작마다 torch 를 띄워 "없음" 을 확인할 이유가 없다).
# 장치를 cpu 등으로 직접 지정했으면 그 지정이 통합 worker(항상 CUDA)에 무시되지 않게 끈다.
COMBINED_GPU_WORKER_ENABLED: bool = os.environ.get(
    "SECUREDOC_COMBINED_GPU_WORKER_ENABLED", "0" if sys.platform == "darwin" else "1"
).strip().lower() in {"1", "true", "yes", "on"} and {PII_DEVICE, INJECTION_DEVICE} <= {"auto", "cuda"}
# 통합 worker 요청 하나에 담는 양 — PII 와 같은 이유(응답 크기·시간 상한)로 문서를 나눠 보낸다.
COMBINED_GPU_PII_WINDOWS_PER_REQUEST: int = 64
COMBINED_GPU_INJECTION_RECORDS_PER_REQUEST: int = 128
# 연속 실패가 이만큼 쌓이면 이번 앱 실행 동안 통합 worker 를 끄고 개별 sidecar 만 쓴다.
COMBINED_GPU_MAX_FAILURES: int = 3
COMBINED_GPU_PYTHON_EXECUTABLE: str = os.environ.get(
    "SECUREDOC_COMBINED_GPU_PYTHON_EXECUTABLE",
    INJECTION_PYTHON_EXECUTABLE,
)
COMBINED_GPU_LOAD_TIMEOUT_SEC: float = float(
    os.environ.get(
        "SECUREDOC_COMBINED_GPU_LOAD_TIMEOUT_SEC",
        str(max(PII_LOAD_TIMEOUT_SEC, INJECTION_LLM_LOAD_DELAY_SEC)),
    )
)

# ── 경로 ──────────────────────────────────────────────────────────────────────
# store 는 "사용자 기기에 쌓이는 것" 이므로 모델 가중치와 같은 자리에 둔다 —
# 앱 번들 안이 아니다.
#
# 예전엔 APP_DIR/store/data, 즉 **앱 번들 내부**였다. macOS 에서 이게 실제로 앱을
# 못 쓰게 만들었다(실사용자 확인):
#   · /Applications/Campfire.app/.../engine/app/store/data/securedoc.sqlite3 가 생기고
#   · codesign --verify 가 "file added" 로 실패한다 = 코드 서명이 깨진다
# 우리 mac 빌드는 진짜 인증서가 없어 ad-hoc 서명(codesign --sign -)만 하는데,
# 그 서명이 첫 실행에서 스스로 무효화된다. 격리(quarantine) 속성까지 살아 있으면
# Gatekeeper 가 개입하고, Apple Silicon 은 서명이 깨진 바이너리 실행을 거부할 수 있다.
# 게다가 번들이 교체·재배치되면 실행 중인 엔진의 CWD 가 사라져 import 도중
# os.getcwd() 가 ENOENT 로 죽는다(실사용자 트레이스백이 정확히 그 모양이었다).
# Windows 에서 같은 코드가 멀쩡했던 건 번들 서명 검증이 없어서일 뿐이다.
#
# 데스크탑 앱은 SECUREDOC_STORE_DIR 로 이 경로를 명시해서 넘긴다 — 앱이 통계를 읽는
# 경로와 엔진이 쓰는 경로가 어긋나면 안 되므로 한쪽(앱)만 정하게 한다. 아래 기본값은
# 엔진을 단독으로 띄웠을 때 쓰이며, 앱이 넘기는 값과 같은 규칙이다.
LEGACY_STORE_DIR: Path = APP_DIR / "store" / "data"
STORE_DIR: Path = Path(
    os.environ.get("SECUREDOC_STORE_DIR", str(_models_base() / "Campfire" / "store"))
)
DB_PATH: Path = STORE_DIR / "securedoc.sqlite3"
AUDIT_LOG_PATH: Path = STORE_DIR / "audit.log"

# ── 파서 지원 범위 (PLAN §6) ───────────────────────────────────────────────────
# U1(TXT/PDF/DOCX) + U5(HWP/HWPX/XLSX/PPTX). HWP 는 LibreOffice 설치 여부에 따라
# 런타임에 동적으로 unsupported 로 떨어질 수 있다(§9.2, §11) — 그래도 "시도는
# 하는" 포맷이므로 여기서는 지원 목록에 둔다.
# XLS/PPT(구버전 바이너리)는 이번 범위 밖이라 UNSUPPORTED 로 유지.
#
# 추가분(의존성 없이 표준 라이브러리만): OpenDocument(odt/ods/odp), 메일(eml/mht),
# HTML, 그리고 평문 계열(csv/tsv/md/json/xml/log). 평문 계열은 예전에도 _looks_textual
# 폴백으로 통과하긴 했지만 목록에 없으면 **확장 프로그램이 가로채질 않아** 검사 자체가
# 일어나지 않았다 — 이 집합은 엔진 능력이자 확장의 가로채기 목록이다.
SUPPORTED_EXTENSIONS: set[str] = {
    ".txt", ".pdf", ".docx", ".hwp", ".hwpx", ".xlsx", ".pptx",
    ".odt", ".ods", ".odp",
    ".eml", ".mht", ".mhtml",
    ".html", ".htm",
    ".csv", ".tsv", ".md", ".json", ".xml", ".log",
}
UNSUPPORTED_EXTENSIONS: set[str] = {".ppt", ".xls"}

# ── 이미지 속 글자 검사 (선택 기능, core/detectors/image_ocr.py) ──────────────────
# 이미지 파일(화면 캡처)과 DOCX·PPTX 속 그림을 NCSOFT VARCO-VISION-2.0-1.7B(CC BY-NC 4.0)로 읽어 본문의 그 자리에 넣고
# 기존 탐지를 그대로 돌린다. 사진으로 찍은 문서는 대상이 아니다(화면 캡처 기준으로 맞춤).
# NVIDIA CUDA·Intel Arc XPU·Apple Silicon MPS에서 돈다. 사이드카가 지원 GPU를 못 찾으면 글자 검사 대상 이미지가 든
# 입력은 막는다(보내지 않는다). Windows 설치본의 torch는 CPU 빌드라, 모델 받기 때 감지한 CUDA/XPU torch를
# GPU_TORCH_DIR에 따로 깔아 사이드카에만 앞세운다(PYTHONPATH, adapters/http_api/models.py). Apple Silicon은 앱에
# 번들된 arm64 torch를 그대로 쓴다. SECUREDOC_IMAGE_OCR_PYTHON을 주면 그 파이썬을 그대로 쓴다.
IMAGE_OCR_ENABLED: bool = os.environ.get("SECUREDOC_IMAGE_OCR_ENABLED", "0") == "1"
IMAGE_OCR_ENGINE_DIR: Path = MODELS_ROOT / "varco_ocr"
IMAGE_OCR_GPU_TORCH_DIR: Path = IMAGE_OCR_ENGINE_DIR / "gpu_torch"
IMAGE_OCR_PYTHON_OVERRIDE: str = os.environ.get("SECUREDOC_IMAGE_OCR_PYTHON", "")
IMAGE_OCR_PYTHON_EXECUTABLE: str = IMAGE_OCR_PYTHON_OVERRIDE or sys.executable
IMAGE_OCR_EXTENSIONS: frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp"})
IMAGE_OCR_BATCH: int = int(os.environ.get("SECUREDOC_IMAGE_OCR_BATCH", "8"))
# 묶음 하나의 (조각 수 × 가장 긴 입력 토큰) 상한 — 입력 토큰당 GPU 메모리 약 0.25MB(실측). 12GB GPU 에 다른 모델과 함께.
IMAGE_OCR_TOKEN_BUDGET: int = int(os.environ.get("SECUREDOC_IMAGE_OCR_TOKEN_BUDGET", "6000"))
# 마지막으로 쓴 뒤 이만큼 지나면 사이드카를 내린다 — 가중치만 약 4.1GB 라 계속 두지 않는다.
IMAGE_OCR_IDLE_UNLOAD_SEC: float = float(os.environ.get("SECUREDOC_IMAGE_OCR_IDLE_UNLOAD_SEC", "120"))
IMAGE_OCR_MAX_IMAGES: int = int(os.environ.get("SECUREDOC_IMAGE_OCR_MAX_IMAGES", "20"))  # 넘으면 나머지는 미검사로 막는다
# 이보다 작은 이미지는 아이콘·로고로 보고 건너뛴다(글자 검사 대상 아님).
IMAGE_OCR_MIN_WIDTH: int = int(os.environ.get("SECUREDOC_IMAGE_OCR_MIN_WIDTH", "120"))
IMAGE_OCR_MIN_HEIGHT: int = int(os.environ.get("SECUREDOC_IMAGE_OCR_MIN_HEIGHT", "32"))

# ── 프록시 게이트웨이 (adapters/proxy) ────────────────────────────────────────
# 확장 프로그램 대신 로컬 TLS 프록시로 업로드를 가로채는 경로. 기본은 꺼짐 —
# 켜려면 mitmproxy 가 필요하고(`pip install -e ".[proxy]"`) 루트 CA 도 깔아야 한다.
PROXY_ENABLED: bool = os.environ.get("SECUREDOC_PROXY_ENABLED", "0") == "1"
PROXY_PORT: int = int(os.environ.get("SECUREDOC_PROXY_PORT", "48210"))

# 프록시가 쓰는 Campfire 전용 루트 CA(adapters/proxy/ca.py) — 검사 대상 AI 도메인으로 이름이 제한된다.
# mitmproxy 기본 위치(~/.mitmproxy)에 알아서 만드는 CA 는 모든 사이트에 쓸 수 있어 쓰지 않는다.
PROXY_CA_DIR: Path = Path(
    os.environ.get("SECUREDOC_PROXY_CA_DIR", str(_models_base() / "Campfire" / "proxy-ca"))
)

# 사람이 검토하는 동안 요청을 붙들고 있는 최대 시간.
#
# 길게 잡고 싶어지는 값이지만 상한은 우리가 정하는 게 아니다 — 사이트 JS 의 abort
# 타임아웃이 먼저 끊으면 사용자는 이유를 알 수 없는 업로드 실패를 본다. 그 값은
# 아직 못 쟀으므로(§실측 문서) 보수적으로 잡고, 재고 나서 올린다.
PROXY_DECISION_TIMEOUT_S: float = float(os.environ.get("SECUREDOC_PROXY_DECISION_TIMEOUT_S", "120"))

# 기본 목록 밖의 호스트도 multipart 검사 대상에 넣는다(쉼표 구분).
# 사이트가 도메인을 바꿨을 때 릴리스 없이 막기 위한 것이고, 검증용 로컬 서버를
# 붙일 때도 쓴다.
PROXY_EXTRA_HOSTS: set[str] = {
    h.strip() for h in os.environ.get("SECUREDOC_PROXY_EXTRA_HOSTS", "").split(",") if h.strip()
}

# 프록시가 본 요청을 한 줄씩 남긴다. 어떤 호스트로 업로드가 가는지 특정할 때 쓴다.
# 기본은 꺼짐 — 켜면 방문 URL 이 로그에 남는다.
PROXY_LOG_REQUESTS: bool = os.environ.get("SECUREDOC_PROXY_LOG_REQUESTS", "0") == "1"

# 프록시가 본 RPC 본문을 이 폴더에 덤프한다(필드 구조 분석용). 기본 꺼짐.
PROXY_CAPTURE_DIR: str = os.environ.get("SECUREDOC_PROXY_CAPTURE_DIR", "")

# ── 라이트 판 (데스크탑 버전에 -lite, desktop/main/edition.js) ─────────────────
# PII·인젝션·사용자 지정 강제 마스킹만 쓴다. 기밀 탐지·프록시·MCP(서버·위험 검사기)·시연 대시보드는
# 위 설정·환경변수와 상관없이 끈다 — 정식 판에서 켜 둔 설정이 남은 채 라이트로 바꿔 설치해도 켜지지 않게.
LITE: bool = os.environ.get("SECUREDOC_EDITION", "") == "lite"
if LITE:
    CONFIDENTIAL_ENABLED = MCP_RISK_SCANNER_ENABLED = DEMO_TRACE_ENABLED = PROXY_ENABLED = IMAGE_OCR_ENABLED = False
if IMAGE_OCR_ENABLED:  # 켰을 때만 이미지 파일이 검사 대상 형식이다 — 꺼져 있으면 지금처럼 미지원(통과)
    SUPPORTED_EXTENSIONS |= IMAGE_OCR_EXTENSIONS
