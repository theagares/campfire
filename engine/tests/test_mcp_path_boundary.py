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

    경계는 _resolve() 에서 지킨다 — 다섯 도구가 모두 인자를 그리로 넣는다. 다만
    _resolve 가 지키는 건 **인자로 받은 경로 하나**뿐이고, rglob 이 돌려주는 항목은
    그 검사를 거치지 않았다(후속 리뷰). 루트 안의 링크가 밖을 가리키면 열거·열람이
    그대로 됐다 — 그쪽은 _iter_root_files 가 막고, 아래 마지막 두 테스트가 지킨다.
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
    _link_dir_or_skip(link, outside_dir)
    with pytest.raises(tools.PathOutsideRootError):
        tools._resolve(str(link / "secret.txt"))


# ── 후속 리뷰: rglob 결과도 경계를 지킨다 ────────────────────────────────────
def _link_dir_or_skip(link: Path, target: Path) -> None:
    """link 가 루트 밖의 target 디렉터리를 가리키게 만든다.

    Windows 에서 symlink 는 권한이 필요해 늘 skip 되는데, **정션(mklink /J)은 권한
    없이 만들어진다** — 즉 이 우회로가 실제로 열려 있는 플랫폼에서 테스트만 건너뛰게
    된다. 그래서 symlink 가 막히면 정션으로 한 번 더 시도한다.
    """
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        import subprocess
        if subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                          capture_output=True).returncode == 0:
            return
    pytest.skip("이 환경에서는 링크를 만들 수 없다")


def test_search_does_not_follow_link_out_of_root(rooted, tmp_path_factory):
    """루트 **안**의 링크가 밖을 가리키면 열거·열람 대상이 되어선 안 된다.

    _resolve 는 root 인자만 검사한다. rglob 결과를 그대로 read_text 에 넘기면
    `ln -s ~/.aws ./notes` 한 줄로 경계가 무의미해진다.
    """
    outside = tmp_path_factory.mktemp("outside")
    (outside / "credentials").write_text("aws_secret_access_key = LEAKEDVALUE123", encoding="utf-8")
    _link_dir_or_skip(rooted / "escape", outside)

    found = [p.name for p in tools._iter_root_files(rooted, "*")]
    assert "credentials" not in found, "루트 밖을 가리키는 링크 안의 파일이 열거됐다"


def test_ordinary_files_still_enumerated(rooted):
    """경계 검사를 넣었다고 멀쩡한 파일까지 사라지면 안 된다."""
    found = [p.name for p in tools._iter_root_files(rooted, "*")]
    assert "doc.txt" in found
