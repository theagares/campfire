"""모델 tar 를 풀 때 dest_dir 밖으로 파일이 나가지 않게 한다 (_safe_extract).

_safe_extract 는 각 멤버의 **경로** 를 검증한다(../ 로 밖을 가리키면 거부). 그런데
링크의 **대상** 은 그 검사에 안 걸린다 — 이름은 dest_dir 안에 있으면서 바깥을
가리키는 심볼릭 링크를 먼저 풀고, 뒤이은 멤버가 그 링크를 통해 쓰면 밖으로 나간다
(고전적 tar 심볼릭 링크 우회).

실현되려면 릴리스가 먼저 털려야 하므로 위험도는 낮다. 다만 막는 값이 한 줄이고,
"경로는 검사했다" 는 사실이 링크까지 안전하다는 뜻으로 읽히기 쉬워서 못 박아 둔다.
"""

from __future__ import annotations

import tarfile

from app.adapters.http_api import models


def _tar_with_symlink(path, name: str, target: str) -> None:
    """이름은 dest 안이지만 대상이 바깥인 심볼릭 링크 하나를 담은 tar."""
    with tarfile.open(path, "w:gz") as tf:
        info = tarfile.TarInfo(name)
        info.type = tarfile.SYMTYPE
        info.linkname = target
        tf.addfile(info)


def _tar_with_file(path, name: str, body: bytes) -> None:
    import io

    with tarfile.open(path, "w:gz") as tf:
        info = tarfile.TarInfo(name)
        info.size = len(body)
        tf.addfile(info, io.BytesIO(body))


def test_symlink_pointing_outside_is_rejected(tmp_path):
    """바깥을 가리키는 심볼릭 링크는 풀리지 않아야 한다."""
    archive = tmp_path / "evil.tar.gz"
    _tar_with_symlink(archive, name="escape", target="../../pwned")
    dest = tmp_path / "dest"
    dest.mkdir()

    raised = None
    with tarfile.open(archive, "r:gz") as tf:
        try:
            models._safe_extract(tf, dest)
        except Exception as exc:  # noqa: BLE001 - 거부되기만 하면 종류는 상관없다
            raised = exc

    assert raised is not None, "바깥을 가리키는 심볼릭 링크가 그대로 풀렸다"
    assert not (dest / "escape").exists(), "링크가 만들어져 뒤이은 멤버가 통과할 수 있다"


def test_path_traversal_still_rejected(tmp_path):
    """기존 경로 검사가 그대로 살아 있어야 한다(필터를 붙이며 깨뜨리지 않았는지)."""
    archive = tmp_path / "trav.tar.gz"
    _tar_with_file(archive, name="../pwned.txt", body=b"x")
    dest = tmp_path / "dest"
    dest.mkdir()

    raised = None
    with tarfile.open(archive, "r:gz") as tf:
        try:
            models._safe_extract(tf, dest)
        except Exception as exc:  # noqa: BLE001
            raised = exc

    assert raised is not None, "../ 경로가 그대로 풀렸다"
    assert not (tmp_path / "pwned.txt").exists()


def test_normal_archive_still_extracts(tmp_path):
    """막느라 정상 모델 tar 까지 못 풀면 앱이 모델을 못 받는다."""
    archive = tmp_path / "ok.tar.gz"
    _tar_with_file(archive, name="weights/model.bin", body=b"hello")
    dest = tmp_path / "dest"
    dest.mkdir()

    with tarfile.open(archive, "r:gz") as tf:
        models._safe_extract(tf, dest)

    assert (dest / "weights" / "model.bin").read_bytes() == b"hello"
