import os
import sys
from pathlib import Path

import pytest

# engine/ 를 import 경로에 추가 (app 패키지)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 리브랜딩 마이그레이션(models_sync._migrate_legacy_models_root)은 사용자 홈의 실제
# 가중치 폴더를 통째로 "옮긴다". 테스트가 앱을 띄우면 lifespan 이 이걸 부르기 때문에,
# 개발 체크아웃에서 pytest 를 돌린 것만으로 설치된 앱의 모델 605MB 가 새 경로로
# 옮겨져 구버전 앱이 모델을 잃는 사고가 실제로 났다. 테스트는 실사용자 상태를
# 건드리지 않는다.
os.environ.setdefault("SECUREDOC_SKIP_LEGACY_MIGRATION", "1")


@pytest.fixture(autouse=True)
def _reset_detector_cache():
    """테스트마다 detector 캐시를 비운다.

    encoder/llm_mcp 는 asyncio 서브프로세스를 들고 있고, 그 transport 는 만들어진
    이벤트 루프에 묶여 있다. 각 테스트는 asyncio.run() 으로 새 루프를 열고 닫으므로,
    앞 테스트가 캐싱해둔 detector 를 그대로 물려받으면 이미 닫힌 루프의 파이프를
    건드려 "서브프로세스가 준비 전 종료됨" / "Event loop is closed" 로 깨진다
    (개별 실행은 통과하는데 전체 실행에서만 5건이 떨어지던 원인).

    룰베이스 폴백이 있던 시절엔 대부분의 테스트가 서브프로세스를 안 띄워 이 문제가
    드러나지 않았다 — 실 모델만 남으면서 표면화됐다.

    reset_cache() 는 살아있는 서브프로세스에 종료 신호까지 보내므로, 테스트 간
    ML 프로세스가 새는 것도 함께 막는다.
    """
    yield
    from app.core.detectors import registry
    registry.reset_cache()
