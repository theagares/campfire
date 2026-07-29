"""
app/config.py
엔진 설정 중앙 관리 (PLAN §11 포트, §9.2 타임아웃, §5 detector 선택).

모든 모듈은 이 파일에서만 설정값을 읽는다. 환경변수로 재정의 가능.
"""

import os
import sys
from pathlib import Path

APP_DIR: Path = Path(__file__).resolve().parent

# ── 서비스 시그니처 (PLAN §11) ────────────────────────────────────────────────
# 익스텐션이 포트 스캔 시 "우리 엔진"임을 식별하는 고정 시그니처.
SERVICE_NAME: str = "securedoc-gateway"

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
# 룰베이스 v1 은 즉시 처리라 사실상 걸릴 일이 없으나, 정책상 임계값을 둔다.
REQUEST_TIMEOUT_SEC: int = int(os.environ.get("SECUREDOC_REQUEST_TIMEOUT_SEC", "30"))

# ── Detector 선택 (PLAN §5 registry) ─────────────────────────────────────────
# v1 은 rule_based 만 존재. encoder / llm_mcp 는 나중에 슬롯만 교체.
PII_DETECTOR: str = os.environ.get("SECUREDOC_PII_DETECTOR", "rule_based")
INJECTION_DETECTOR: str = os.environ.get("SECUREDOC_INJECTION_DETECTOR", "rule_based")

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
INJECTION_ENGINE_DIR: Path = APP_DIR / "models" / "injection_engine"
INJECTION_VARIANT: str = os.environ.get("SECUREDOC_INJECTION_VARIANT", "hybrid")  # "hybrid" | "attn"
INJECTION_DEVICE: str = os.environ.get("SECUREDOC_INJECTION_DEVICE", "cuda")
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
    dotenv_path = APP_DIR.parent.parent / ".env"  # engine/app -> engine -> securedoc-gateway/.env
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
PII_ENGINE_DIR: Path = APP_DIR / "models" / "pii_engine"
PII_MODEL_SEED: str = os.environ.get("SECUREDOC_PII_MODEL_SEED", "seed42")
# GPU 실측(1,500자 청크 기준): CPU 1.69초 vs GPU 0.43초(~4배), VRAM 피크 1.74GB —
# 인젝션 LLM(피크 3.8GB)과 동시 로드해도 합계 ~5.5GB 로 6GB+ VRAM 환경에선 여유 있음.
PII_DEVICE: str = os.environ.get("SECUREDOC_PII_DEVICE", "cuda")
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
