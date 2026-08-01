"""
app/models_sync.py
앱과 함께 배포되는 모델 지원 파일(런타임 스크립트·설정)을 영구 보관 위치로 맞춘다.

왜 이런 게 필요한가: 내려받은 가중치는 앱 설치 폴더 밖(config.MODELS_ROOT)에 둔다.
설치 폴더는 재설치·업데이트 때 통째로 교체되므로 거기 두면 앱을 다시 받을 때마다 이미
갖고 있는 600MB 를 또 받게 된다(실사용자 리포트). 반대로 런타임 스크립트는 앱 버전과
함께 가야 하므로 계속 설치 폴더에서 배포된다 — 그래서 기동 시 그 작은 파일들만 보관
위치로 복사해 둘을 합쳐준다.

즉 경계는 이렇다:
  BUNDLED_MODELS_DIR (앱과 함께, 매 설치마다 교체) : runtime/*.py, extract_config.json 등
  MODELS_ROOT        (사용자 기기에 누적, 재설치해도 유지) : 내려받은 가중치
"""

from __future__ import annotations

import shutil
from pathlib import Path

from app import config

# 이 이름들은 "가중치" 라 덮어쓰면 안 된다 — 나머지(앱이 주는 지원 파일)만 동기화한다.
_WEIGHT_DIR_NAMES = {"models", "hybrid", "attn"}


def _sync_dir(bundled: Path, persistent: Path) -> None:
    """bundled 안의 지원 파일을 persistent 로 복사(앱 버전이 이긴다).

    가중치 디렉터리는 건드리지 않는다. 앱 번들에는 어차피 가중치가 들어있지 않지만
    (CI 빌드 시점엔 내려받기 전이라), 개발자가 로컬에서 받아둔 채로 빌드한 번들이
    섞여 들어와 사용자가 이미 가진 가중치를 덮어쓰는 일이 없게 명시적으로 제외한다.
    """
    if not bundled.is_dir():
        return
    persistent.mkdir(parents=True, exist_ok=True)
    for item in bundled.iterdir():
        if item.name in _WEIGHT_DIR_NAMES:
            continue
        target = persistent / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)


def _migrate_legacy_weights(bundled: Path, persistent: Path) -> None:
    """예전 위치(앱 설치 폴더 안)에 이미 받아둔 가중치가 있으면 보관 위치로 옮긴다.

    이 함수가 없으면, 이 변경이 배포된 직후 기존 사용자는 "예전 위치엔 있는데 새 위치엔
    없다" 는 이유로 600MB 를 한 번 더 받게 된다. 복사가 아니라 이동이라 디스크도 두 배로
    쓰지 않는다(같은 볼륨이면 사실상 즉시).
    """
    for name in _WEIGHT_DIR_NAMES:
        src = bundled / name
        dst = persistent / name
        if not src.is_dir() or dst.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(src), str(dst))
        except OSError:
            # 볼륨이 다르거나 권한이 없으면 그냥 포기한다 — 최악의 경우 다시 받을 뿐이라
            # 기동을 막을 이유가 없다.
            pass


def sync_bundled_model_files() -> None:
    """엔진 기동 시 1회. 실패해도 기동을 막지 않는다(가중치는 어차피 재다운로드 가능)."""
    for bundled_name, persistent in (
        ("pii_engine", config.PII_ENGINE_DIR),
        ("injection_engine", config.INJECTION_ENGINE_DIR),
    ):
        bundled = config.BUNDLED_MODELS_DIR / bundled_name
        try:
            _migrate_legacy_weights(bundled, persistent)
            _sync_dir(bundled, persistent)
        except Exception:  # noqa: BLE001 - 지원 파일 동기화 실패로 엔진을 못 뜨게 하지 않는다
            pass
