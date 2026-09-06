"""MCP 파일 도구가 작업 루트를 벗어나지 못하게 지킨다.

왜 생겼나 (2026-09-06):
    _PROJECT_ROOT 는 "상대 경로를 붙이는 기준" 으로만 쓰였고 경계로 강제되지 않았다.
    절대 경로는 그대로 통과했고 `..` 도 막히지 않아, 파일 도구 다섯 개
    (secure_read_file / secure_list_files / secure_search_files / scan_file /
    scan_files)로 디스크 어디든 읽고 열거할 수 있었다.

    이 도구를 부르는 쪽은 사람이 아니라 AI 에이전트다. 브라우저 확장 경로처럼
    검토 패널에서 사람이 승인하는 단계가 없다. 게다가 이름이 secure_* 라 에이전트도
    사용자도 "걸러진 값" 으로 취급한다 — 실제로는 PII 만 가리므로, 예컨대 .env 나
    개인키를 읽히면 자격증명이 그대로 나간다(별건으로 추적).

    경계는 _resolve() 한 곳에서만 지키면 된다. 다섯 도구가 모두 그리로 들어온다.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from app.adapters.mcp import tools


@pytest.fixture()
def rooted(tmp_path, monkeypatch):
    """tmp_path 를 작업 루트로 쓰는 _resolve 를 돌려준다."""
    root = tmp_path.resolve()
    (root / "inside").mkdir()
    (root / "inside" / "doc.txt").write_text("hello", encoding="utf-8")
    monkeypatch.setattr(tools, "_PROJECT_ROOT", root)
    return root


def test_relative_path_resolves_under_root(rooted):
    assert tools._resolve("inside/doc.txt") == rooted / "inside" / "doc.txt"


def test_root_itself_is_allowed(rooted):
    # secure_list_files(root=".") 가 기본값으로 쓰는 경로다.
    assert tools._resolve(".") == rooted


def test_absolute_path_inside_root_is_allowed(rooted):
    assert tools._resolve(str(rooted / "inside")) == rooted / "inside"


def test_absolute_path_outside_root_is_rejected(rooted, tmp_path_factory):
    """루트 밖 절대 경로 — 에이전트가 ~/.ssh/id_rsa 를 요구하는 그 경우."""
    outside = tmp_path_factory.mktemp("outside") / "secret.txt"
    outside.write_text("SECRET", encoding="utf-8")
    with pytest.raises(tools.PathOutsideRootError):
        tools._resolve(str(outside))


def test_dotdot_traversal_is_rejected(rooted):
    with pytest.raises(tools.PathOutsideRootError):
        tools._resolve(os.path.join("..", "..", "etc", "passwd"))


def test_sibling_with_shared_prefix_is_rejected(rooted):
    """/srv/app 이 /srv/apple 을 통과시키던 접두 비교 실수를 막는다.

    구분자를 붙이지 않고 startswith 로만 비교하면 이 케이스가 새어 들어온다.
    """
    sibling = Path(str(rooted) + "x") / "doc.txt"
    with pytest.raises(tools.PathOutsideRootError):
        tools._resolve(str(sibling))


@pytest.mark.skipif(os.name != "nt", reason="Windows 경로만 대소문자를 구분하지 않는다")
def test_case_insensitive_on_windows(rooted):
    """Path.is_relative_to 로 비교하면 대소문자가 달라 멀쩡한 경로가 거부된다."""
    swapped = str(rooted / "inside").swapcase()
    # 대소문자만 다를 뿐 같은 위치이므로 통과해야 한다.
    assert tools._within_root(Path(swapped))


def test_symlink_escaping_root_is_rejected(rooted, tmp_path_factory):
    """링크는 .resolve() 로 풀린 뒤 판정돼야 한다 — 루트 안에 두고 밖을 가리켜도 거부."""
    outside_dir = tmp_path_factory.mktemp("linked")
    link = rooted / "escape"
    try:
        link.symlink_to(outside_dir, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("이 환경에서는 심볼릭 링크를 만들 수 없다(Windows 권한 등)")
    with pytest.raises(tools.PathOutsideRootError):
        tools._resolve(str(link / "secret.txt"))
