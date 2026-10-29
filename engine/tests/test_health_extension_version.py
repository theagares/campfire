"""설치된 확장 판은 Chrome 패키지 캐시가 아니라 실제 manifest에서 매번 읽는다."""

from app import config
from app.adapters.http_api.health import _installed_extension_version


def test_installed_extension_version_tracks_disk_changes(monkeypatch, tmp_path):
    extension_dir = tmp_path / "extension"
    extension_dir.mkdir()
    manifest = extension_dir / "manifest.json"
    monkeypatch.setattr(config, "EXTENSION_DIR", extension_dir)

    manifest.write_text('{"version":"0.2.58"}', encoding="utf-8")
    assert _installed_extension_version() == "0.2.58"

    manifest.write_text('{"version":"0.2.58.1"}', encoding="utf-8")
    assert _installed_extension_version() == "0.2.58.1"

    manifest.write_text('{"version":', encoding="utf-8")
    assert _installed_extension_version() is None
