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

# 실제 바인딩된 포트. main.py 가 기동 시점에 채운다. /health 가 이 값을 반환.
BOUND_PORT: int | None = None

# ── 파이프라인 (PLAN §6) ──────────────────────────────────────────────────────
# 실측(인젝션 LLM, 6,000자 문서 기준): 1,500자 청크(5개)=0.36초/3.67GB peak vs
# 1,000자 청크(7개)=0.34초/3.09GB peak — 1,000자가 속도·VRAM 둘 다 더 나은
# 유일한 지점(그 이하로 더 쪼개면 청크당 고정 오버헤드가 누적돼 총 시간이 늘어남).
CHUNK_SIZE: int = int(os.environ.get("SECUREDOC_CHUNK_SIZE", "1000"))

# ── 요청 제한 ─────────────────────────────────────────────────────────────────
MAX_UPLOAD_BYTES: int = int(os.environ.get("SECUREDOC_MAX_UPLOAD_BYTES", str(20 * 1024 * 1024)))
MAX_PROMPT_CHARS: int = int(os.environ.get("SECUREDOC_MAX_PROMPT_CHARS", "100000"))

# ── 타임아웃 (PLAN §9.2) ──────────────────────────────────────────────────────
REQUEST_TIMEOUT_SEC: int = int(os.environ.get("SECUREDOC_REQUEST_TIMEOUT_SEC", "30"))

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
PII_DETECTOR: str = os.environ.get("SECUREDOC_PII_DETECTOR", "encoder")
INJECTION_DETECTOR: str = os.environ.get("SECUREDOC_INJECTION_DETECTOR", "llm_mcp")

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


# ── 인젝션 2단계 세부 위치 특정 (Upstage Solar Pro 3) ─────────────────────────
# EXAONE hybrid 분류기는 청크 전체를 misaligned/aligned/non_instruction 로만
# 판정하고 청크 내 구체적 위치는 모르는 구조라(어텐션/hidden state 를 청크 전체에
# 걸쳐 풀링해서 분류), misaligned 판정 시 청크 전체가 통째로 마스킹된다. Solar
# Pro 3 에 "이 청크에서 실제 인젝션 지시문이 정확히 어느 부분이냐"를 다시 물어
# 그 부분만 정밀하게 마스킹하기 위한 2단계 호출. 1단계(EXAONE)가 이미
# misaligned 라고 판정한 청크에 대해서만 호출하므로 비용/지연이 항상 붙지 않는다.
# 실패/애매하면(빈 응답, API 오류, 응답이 원문과 정확히 일치하지 않음) 기존처럼
# 청크 전체를 마스킹하는 fail-safe 로 되돌아간다(검사 자체를 생략하지 않음).
UPSTAGE_API_KEY: str = (
    os.environ.get("UPSTAGE_API_KEY")
    or os.environ.get("SECUREDOC_UPSTAGE_API_KEY")
    or _load_dotenv_value("upstage_key")
)
UPSTAGE_API_BASE: str = os.environ.get(
    "SECUREDOC_UPSTAGE_API_BASE", "https://api.upstage.ai/v1/solar/chat/completions"
)
UPSTAGE_MODEL: str = os.environ.get("SECUREDOC_UPSTAGE_MODEL", "solar-pro3")
UPSTAGE_TIMEOUT_SEC: float = float(os.environ.get("SECUREDOC_UPSTAGE_TIMEOUT_SEC", "20"))
# API 키가 없으면(로컬 전용 배포 등) 자동으로 비활성화 — 2단계 없이 기존 청크
# 전체 마스킹 동작 그대로 유지.
INJECTION_LOCALIZE_ENABLED: bool = bool(UPSTAGE_API_KEY)

# ── PII 인코더 실제 모델 (skt/A.X-Encoder-base + CRF + gazetteer 단일 모델,
#   hwan님이 준비한 pii_skt_crf_gaz_mix_all_x3_local_app 번들의 seed42 를 로컬
#   서브프로세스 sidecar 로 실행) ─────────────────────────────────────────────
# 원래 3-seed(seed42/43/44) 앙상블로 구동했으나, 번들의 세 seed 가 실제로는
# 완전히 동일한 가중치(하드링크)로 패키징된 버그가 확인되어 앙상블의 다양성
# 이득이 전혀 없었음 — 단일 seed 로 전환하기로 결정(연산량 3배 낭비 제거).
PII_ENGINE_DIR: Path = MODELS_ROOT / "pii_engine"
PII_MODEL_SEED: str = os.environ.get("SECUREDOC_PII_MODEL_SEED", "seed42")
# GPU 실측(1,500자 청크 기준): CPU 1.69초 vs GPU 0.43초(~4배), VRAM 피크 1.74GB —
# 인젝션 LLM(피크 3.8GB)과 동시 로드해도 합계 ~5.5GB 로 6GB+ VRAM 환경에선 여유 있음.
# 기본값 "auto" — INJECTION_DEVICE와 동일한 이유(위 주석 참고): CPU 전용 torch가
# 배포됐을 때도 죽지 않고 CPU로 폴백하게 한다.
PII_DEVICE: str = os.environ.get("SECUREDOC_PII_DEVICE", "auto")
PII_BATCH_SIZE: int = int(os.environ.get("SECUREDOC_PII_BATCH_SIZE", "8"))
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

# ── 경로 ──────────────────────────────────────────────────────────────────────
RULES_DIR: Path = APP_DIR / "rules"
STORE_DIR: Path = Path(os.environ.get("SECUREDOC_STORE_DIR", str(APP_DIR / "store" / "data")))
DB_PATH: Path = STORE_DIR / "securedoc.sqlite3"
AUDIT_LOG_PATH: Path = STORE_DIR / "audit.log"

# ── 파서 지원 범위 (PLAN §6) ───────────────────────────────────────────────────
# U1(TXT/PDF/DOCX) + U5(HWP/HWPX/XLSX/PPTX). HWP 는 LibreOffice 설치 여부에 따라
# 런타임에 동적으로 unsupported 로 떨어질 수 있다(§9.2, §11) — 그래도 "시도는
# 하는" 포맷이므로 여기서는 지원 목록에 둔다.
# XLS/PPT(구버전 바이너리)는 이번 범위 밖이라 UNSUPPORTED 로 유지.
SUPPORTED_EXTENSIONS: set[str] = {".txt", ".pdf", ".docx", ".hwp", ".hwpx", ".xlsx", ".pptx"}
UNSUPPORTED_EXTENSIONS: set[str] = {".ppt", ".xls"}
