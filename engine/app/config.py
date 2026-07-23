"""
app/config.py
엔진 설정 중앙 관리 (PLAN §11 포트, §9.2 타임아웃, §5 detector 선택).

모든 모듈은 이 파일에서만 설정값을 읽는다. 환경변수로 재정의 가능.
"""

import os
from pathlib import Path

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
    os.environ.get("SECUREDOC_INJECTION_LLM_LOAD_DELAY_SEC", "1.5")
)  # 콜드 스타트 지연 흉내(가짜 로드). 실제 모델 도입 시 실제 로드 시간으로 대체.

# ── 경로 ──────────────────────────────────────────────────────────────────────
APP_DIR: Path = Path(__file__).resolve().parent
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
