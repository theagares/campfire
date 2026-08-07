"""store(탐지 통계 DB + audit.log)가 앱 번들 안에 쓰이지 않는지 지킨다.

배경(실사용자, 0.2.17 을 신규 설치한 mac):
  ls  /Applications/Campfire.app/.../engine/app/store/data/securedoc.sqlite3   → 있음
  codesign --verify /Applications/Campfire.app
      → "file added: .../.venv/lib/python3.11/re/__pycache__/_compiler.cpython-311.pyc" …
  xattr -p com.apple.quarantine /Applications/Campfire.app → 살아 있음

앱이 자기 번들 안에 파일을 쓰면 코드 서명이 깨진다. 우리 mac 빌드는 진짜 인증서가
없어 ad-hoc 서명(codesign --sign -)만 하므로, 그 서명이 첫 실행에서 스스로 무효화된다.
격리 속성까지 살아 있으면 Gatekeeper 가 개입하고, Apple Silicon 은 서명이 깨진
바이너리 실행을 거부할 수 있다. 게다가 번들이 교체·재배치되면 실행 중인 엔진의 CWD 가
사라져 import 도중 os.getcwd() 가 ENOENT 로 죽는다(실사용자 트레이스백이 그 모양이었다).

Windows 에서 같은 코드가 멀쩡했던 건 번들 서명 검증이 없어서일 뿐이다 — 즉 이건
"mac 전용 버그" 가 아니라 어디서나 틀린 설계였고 mac 에서만 증상이 났다.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path

from app import config
from app.store import db as store_db


def test_기본_store_위치는_앱_번들_밖이다():
    """SECUREDOC_STORE_DIR 을 안 줬을 때의 **실제** 기본값을 본다.

    conftest 가 테스트용 STORE_DIR 을 넣어두므로 그냥 config.STORE_DIR 을 읽으면
    기본값이 아니다. 기대값을 테스트에서 다시 계산하면 config.py 가 무엇을 하든
    항상 통과하는 무의미한 테스트가 된다(실제로 처음 그렇게 썼다가 되돌리기 실험에서
    걸렸다). 그래서 환경변수를 걷어내고 config 를 다시 읽어 진짜 기본값을 확인한다.
    """
    saved = os.environ.get("SECUREDOC_STORE_DIR")
    try:
        os.environ.pop("SECUREDOC_STORE_DIR", None)
        reloaded = importlib.reload(config)
        default_dir = reloaded.STORE_DIR
        assert not str(default_dir).startswith(str(reloaded.APP_DIR)), (
            f"기본 store 위치가 앱 코드 디렉터리 안이다: {default_dir} — "
            "mac 에서 번들에 쓰면 코드 서명이 깨진다"
        )
        # 모델 가중치와 같은 뿌리에 둔다 — 사용자 데이터가 두 군데로 흩어지지 않게.
        assert default_dir.parent == reloaded._default_models_root().parent
    finally:
        if saved is not None:
            os.environ["SECUREDOC_STORE_DIR"] = saved
        importlib.reload(config)  # 다른 테스트가 쓰는 상태로 되돌린다


def test_예전_위치의_기록을_이어붙이되_원본을_지우지_않는다(tmp_path, monkeypatch):
    """옮기지 않고 복사한다.

    모델 마이그레이션이 shutil.move 를 쓰는 바람에 개발 체크아웃에서 pytest 를 돌린
    것만으로 설치된 앱의 605MB 가중치가 사라진 사고가 났다. 여기서 지키려는 건 탐지
    통계뿐이라 원본을 지울 이유가 없다.
    """
    legacy = tmp_path / "bundle" / "store" / "data"
    legacy.mkdir(parents=True)
    (legacy / "securedoc.sqlite3").write_bytes(b"OLD-DB")
    (legacy / "audit.log").write_text("old-audit\n", encoding="utf-8")

    new_dir = tmp_path / "userdata" / "store"
    monkeypatch.setattr(config, "LEGACY_STORE_DIR", legacy)
    monkeypatch.setattr(config, "STORE_DIR", new_dir)
    monkeypatch.setattr(config, "DB_PATH", new_dir / "securedoc.sqlite3")
    monkeypatch.setattr(config, "AUDIT_LOG_PATH", new_dir / "audit.log")
    monkeypatch.delenv("SECUREDOC_SKIP_LEGACY_MIGRATION", raising=False)

    store_db._adopt_legacy_store()

    assert (new_dir / "securedoc.sqlite3").read_bytes() == b"OLD-DB"
    assert (new_dir / "audit.log").read_text(encoding="utf-8") == "old-audit\n"
    # 원본은 그대로 남아 있어야 한다 — 옮겼다면 여기서 걸린다.
    assert (legacy / "securedoc.sqlite3").is_file()


def test_새_위치에_이미_있으면_예전_것으로_덮지_않는다(tmp_path, monkeypatch):
    legacy = tmp_path / "bundle" / "store" / "data"
    legacy.mkdir(parents=True)
    (legacy / "securedoc.sqlite3").write_bytes(b"OLD-DB")

    new_dir = tmp_path / "userdata" / "store"
    new_dir.mkdir(parents=True)
    (new_dir / "securedoc.sqlite3").write_bytes(b"NEW-DB")

    monkeypatch.setattr(config, "LEGACY_STORE_DIR", legacy)
    monkeypatch.setattr(config, "STORE_DIR", new_dir)
    monkeypatch.setattr(config, "DB_PATH", new_dir / "securedoc.sqlite3")
    monkeypatch.setattr(config, "AUDIT_LOG_PATH", new_dir / "audit.log")
    monkeypatch.delenv("SECUREDOC_SKIP_LEGACY_MIGRATION", raising=False)

    store_db._adopt_legacy_store()

    assert (new_dir / "securedoc.sqlite3").read_bytes() == b"NEW-DB"


def test_실제_기동_경로가_번들에_쓰지_않는다(tmp_path, monkeypatch):
    """init_db 가 config.STORE_DIR 에만 쓴다 — 코드 디렉터리는 건드리지 않는다."""
    new_dir = tmp_path / "store"
    monkeypatch.setattr(config, "STORE_DIR", new_dir)
    monkeypatch.setattr(config, "DB_PATH", new_dir / "securedoc.sqlite3")
    monkeypatch.setattr(config, "AUDIT_LOG_PATH", new_dir / "audit.log")
    monkeypatch.setattr(config, "LEGACY_STORE_DIR", tmp_path / "does-not-exist")

    before = _snapshot(config.APP_DIR / "store")
    store_db.init_db()
    try:
        assert (new_dir / "securedoc.sqlite3").is_file()
        assert _snapshot(config.APP_DIR / "store") == before, (
            "앱 코드 디렉터리 안에 파일이 새로 생겼다 — mac 에서 코드 서명이 깨지는 그 경로다"
        )
    finally:
        store_db._conn = None


def _snapshot(root: Path) -> set[str]:
    if not root.is_dir():
        return set()
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}
