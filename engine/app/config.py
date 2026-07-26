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
CHUNK_SIZE: int = int(os.environ.get("SECUREDOC_CHUNK_SIZE", "1500"))  # 청크 분할 1,500자

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

# ── 인젝션 LLM 실제 모델 (EXAONE-3.5-2.4B-Instruct + regularized MLP, hwan님 GPU
#   서버에서 학습한 injection_exaone_regularized_mlp_engine 번들을 로컬 서브프로세스
#   sidecar 로 실행) ────────────────────────────────────────────────────────────
INJECTION_ENGINE_DIR: Path = APP_DIR / "models" / "injection_engine"
INJECTION_BACKEND_KEY: str = os.environ.get("SECUREDOC_INJECTION_BACKEND_KEY", "exaone_3_5_2_4b_instruct")
INJECTION_VARIANT: str = os.environ.get("SECUREDOC_INJECTION_VARIANT", "enc")
INJECTION_DETECTOR_NAME: str = os.environ.get("SECUREDOC_INJECTION_DETECTOR_NAME", "pooled")
INJECTION_DEVICE: str = os.environ.get("SECUREDOC_INJECTION_DEVICE", "cuda")
# "auto"(accelerate device_map)는 Windows 에서 safetensors 의 device-mapped
# fast-load 경로가 페이지 파일 크기에 따라 "OS error 1455"로 실패하는 경우가
# 있어(로컬 실측), 기본값을 "none"(전체 CPU 로드 후 .to(device))으로 둔다.
INJECTION_DEVICE_MAP: str = os.environ.get("SECUREDOC_INJECTION_DEVICE_MAP", "none")
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

# ── PII 인코더 실제 모델 (skt/A.X-Encoder-base + CRF + gazetteer, 3-seed 앙상블,
#   hwan님이 준비한 pii_skt_crf_gaz_mix_all_x3_local_app 번들을 로컬 서브프로세스
#   sidecar 로 실행) ────────────────────────────────────────────────────────────
PII_ENGINE_DIR: Path = APP_DIR / "models" / "pii_engine"
PII_DEVICE: str = os.environ.get("SECUREDOC_PII_DEVICE", "cpu")  # CPU 로 충분히 가벼움 — GPU 는 인젝션 LLM 전용으로 비워둠
PII_MIN_VOTES: int = int(os.environ.get("SECUREDOC_PII_MIN_VOTES", "2"))  # 3개 seed 중 과반 투표
PII_BATCH_SIZE: int = int(os.environ.get("SECUREDOC_PII_BATCH_SIZE", "8"))
PII_MAX_LENGTH: int = int(os.environ.get("SECUREDOC_PII_MAX_LENGTH", "256"))  # 토큰 기준(모델 권장 기본값)
# 위 PII_MAX_LENGTH(토큰)보다 청크가 길면 뒷부분이 잘리므로, detect() 내부에서
# 문자 기준 슬라이딩 윈도우로 잘라 여러 건을 한 번에 배치 추론한다.
PII_WINDOW_SIZE: int = int(os.environ.get("SECUREDOC_PII_WINDOW_SIZE", "200"))
PII_WINDOW_OVERLAP: int = int(os.environ.get("SECUREDOC_PII_WINDOW_OVERLAP", "30"))
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
