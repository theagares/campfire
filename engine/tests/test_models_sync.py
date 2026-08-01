"""모델 지원 파일 동기화 + 예전 위치 가중치 이관 (app/models_sync.py).

핵심 불변식: 앱을 다시 설치해도 이미 받아둔 가중치를 다시 받지 않는다.
설치 폴더는 재설치 때 통째로 교체되므로 가중치는 그 밖(MODELS_ROOT)에 두고,
앱이 주는 런타임 스크립트만 그쪽으로 복사해 합친다.
"""

import json

from app import config, models_sync


def _setup(tmp_path, monkeypatch, *, legacy_weights=False):
    bundled = tmp_path / "bundled"
    persistent = tmp_path / "persistent"
    for name in ("pii_engine", "injection_engine"):
        d = bundled / name
        (d / "runtime").mkdir(parents=True)
        (d / "runtime" / "infer.py").write_text("print('v2')", encoding="utf-8")
    (bundled / "injection_engine" / "extract_config.json").write_text(
        json.dumps({"model": "LGAI-EXAONE/EXAONE-4.0-1.2B"}), encoding="utf-8"
    )
    if legacy_weights:
        # 예전 배포처럼 앱 설치 폴더 안에 가중치가 이미 있는 상태
        (bundled / "pii_engine" / "models" / "seed42").mkdir(parents=True)
        (bundled / "pii_engine" / "models" / "seed42" / "model.safetensors").write_text("W", encoding="utf-8")
        (bundled / "injection_engine" / "hybrid").mkdir(parents=True)
        (bundled / "injection_engine" / "hybrid" / "model.pt").write_text("W", encoding="utf-8")

    monkeypatch.setattr(config, "BUNDLED_MODELS_DIR", bundled)
    monkeypatch.setattr(config, "PII_ENGINE_DIR", persistent / "pii_engine")
    monkeypatch.setattr(config, "INJECTION_ENGINE_DIR", persistent / "injection_engine")
    return bundled, persistent


def test_runtime_files_are_copied_to_persistent_dir(tmp_path, monkeypatch):
    _, persistent = _setup(tmp_path, monkeypatch)
    models_sync.sync_bundled_model_files()
    assert (persistent / "pii_engine" / "runtime" / "infer.py").is_file()
    assert (persistent / "injection_engine" / "extract_config.json").is_file()


def test_existing_weights_are_not_redownloaded(tmp_path, monkeypatch):
    """이 테스트가 이 변경의 목적 그 자체 — 이미 있는 가중치는 건드리지 않는다."""
    bundled, persistent = _setup(tmp_path, monkeypatch)
    seed = persistent / "pii_engine" / "models" / "seed42"
    seed.mkdir(parents=True)
    (seed / "model.safetensors").write_text("ALREADY", encoding="utf-8")

    models_sync.sync_bundled_model_files()

    assert (seed / "model.safetensors").read_text(encoding="utf-8") == "ALREADY"


def test_bundled_weights_never_overwrite_user_weights(tmp_path, monkeypatch):
    """개발자가 가중치를 받아둔 채로 빌드해 번들에 섞여 들어와도 사용자 것이 이긴다."""
    bundled, persistent = _setup(tmp_path, monkeypatch, legacy_weights=True)
    seed = persistent / "pii_engine" / "models" / "seed42"
    seed.mkdir(parents=True)
    (seed / "model.safetensors").write_text("USER", encoding="utf-8")

    models_sync.sync_bundled_model_files()

    assert (seed / "model.safetensors").read_text(encoding="utf-8") == "USER"


def test_legacy_weights_are_migrated_not_redownloaded(tmp_path, monkeypatch):
    """이 변경 직후, 예전 위치에만 가중치가 있는 기존 사용자가 다시 받지 않게 한다."""
    bundled, persistent = _setup(tmp_path, monkeypatch, legacy_weights=True)

    models_sync.sync_bundled_model_files()

    assert (persistent / "pii_engine" / "models" / "seed42" / "model.safetensors").is_file()
    assert (persistent / "injection_engine" / "hybrid" / "model.pt").is_file()


def test_missing_bundle_does_not_raise(tmp_path, monkeypatch):
    """지원 파일 동기화 실패가 엔진 기동 자체를 막으면 안 된다."""
    monkeypatch.setattr(config, "BUNDLED_MODELS_DIR", tmp_path / "does_not_exist")
    monkeypatch.setattr(config, "PII_ENGINE_DIR", tmp_path / "p" / "pii_engine")
    monkeypatch.setattr(config, "INJECTION_ENGINE_DIR", tmp_path / "p" / "injection_engine")
    models_sync.sync_bundled_model_files()  # 예외 없이 끝나야 한다
