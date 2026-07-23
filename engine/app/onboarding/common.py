"""
app/onboarding/common.py
온보딩 체크리스트 공용 유틸 (PLAN §4.2).

안전 수칙(필독): apply_diff() 는 apply=True 를 명시적으로 받았을 때만
diff.path 에 파일을 쓴다. 기본은 항상 dry-run(diff 생성만, 파일시스템 미변경).
어떤 함수도 실제 홈 디렉토리를 테스트 기본값으로 사용해서는 안 된다 — 호출자가
항상 대상 경로를 인자로 주입해야 한다(이 리포지토리의 테스트는 tempfile 로 만든
임시 디렉토리만 넘긴다).
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


class MutationError(Exception):
    """mutate 콜백이 예상 형식 불일치 등으로 안전하게 수정할 수 없을 때 발생시킨다."""


@dataclass
class SettingsDiff:
    """설정 파일 하나에 대한 dry-run diff 결과.

    apply_diff(diff, apply=True) 를 명시적으로 호출하기 전까지는 어떤 파일도
    실제로 변경되지 않는다 — 이 객체는 순수하게 "무엇을 바꿀 것인가"의 표현이다.
    """

    path: Path
    exists_before: bool
    before: dict | None
    after: dict | None
    changed: bool
    note: str
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "existsBefore": self.exists_before,
            "before": self.before,
            "after": self.after,
            "changed": self.changed,
            "note": self.note,
            "error": self.error,
        }


def load_json(path: Path) -> tuple[dict | None, str | None]:
    """JSON 파일을 읽는다. 파싱 실패해도 예외로 죽지 않고 (None, error) 를 반환한다.

    파일이 없으면 (None, None) — "새로 만들어야 함" 상태.
    """
    if not path.exists():
        return None, None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"파일 읽기 실패: {exc}"
    if not text.strip():
        return {}, None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"JSON 파싱 실패: {exc}"
    if not isinstance(data, dict):
        return None, "최상위가 객체(dict)가 아닙니다."
    return data, None


def build_diff(
    target_path: Path,
    mutate: Callable[[dict], tuple[bool, str]],
    *,
    parse_error_note: str = "기존 설정 파일을 파싱할 수 없어 자동으로 수정하지 않습니다. 수동 확인이 필요합니다.",
) -> SettingsDiff:
    """target_path 의 JSON 을 읽어 mutate(after) -> (changed, note) 를 적용한 diff 를 생성한다.

    - mutate 는 after 딕셔너리를 in-place 로 수정하고 (changed, note) 를 반환한다.
      형식이 예상과 달라 안전하게 수정할 수 없으면 MutationError 를 던진다.
    - 파일이 없으면 빈 딕셔너리에서 시작한다(= 신규 생성 diff).
    - JSON 파싱 실패 시 after=None 으로 반환(적용 불가 — 사람이 확인해야 함).
    - 이 함수는 파일을 절대 쓰지 않는다(diff 생성만). 실제 적용은 apply_diff() 몫.
    """
    target_path = Path(target_path)
    exists_before = target_path.exists()
    before, error = load_json(target_path)

    if error is not None:
        return SettingsDiff(
            path=target_path,
            exists_before=exists_before,
            before=None,
            after=None,
            changed=False,
            note=parse_error_note,
            error=error,
        )

    after = copy.deepcopy(before) if before is not None else {}
    try:
        changed, note = mutate(after)
    except MutationError as exc:
        return SettingsDiff(
            path=target_path,
            exists_before=exists_before,
            before=before,
            after=None,
            changed=False,
            note=str(exc),
            error=str(exc),
        )
    return SettingsDiff(
        path=target_path,
        exists_before=exists_before,
        before=before,
        after=after,
        changed=changed,
        note=note,
    )


def apply_diff(diff: SettingsDiff, *, apply: bool = False) -> bool:
    """apply=True 일 때만 실제로 diff.path 에 쓴다 (기본 dry-run, 아무 것도 하지 않음).

    호출자가 명시적으로 apply=True 를 넘기지 않는 한 파일시스템에 절대 쓰지 않는다 —
    이 안전장치 덕분에 diff 생성 함수를 아무리 호출해도(예: checklist 보고서 생성)
    실제 ~/.claude/settings.json 등을 실수로 덮어쓸 위험이 구조적으로 차단된다.
    """
    if not apply or not diff.changed or diff.after is None:
        return False
    diff.path.parent.mkdir(parents=True, exist_ok=True)
    diff.path.write_text(json.dumps(diff.after, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True
