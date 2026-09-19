"""Human-readable risk report; the JSON form remains available for tooling."""

from __future__ import annotations

from .core import CAPS, Report

LABELS = {
    "tool_poisoning": "도구 설명 오염",
    "permission_scope": "권한 범위",
    "file_system_access": "파일 접근",
    "network_access": "네트워크 접근",
    "secret_access": "비밀정보 접근",
    "dependency_risk": "의존성 위험",
    "prompt_injection": "프롬프트 인젝션",
    "runtime_behavior": "실행 중 관찰",
}
VERDICT_LABELS = {
    "critical": "중대 위험",
    "high_risk": "높은 위험",
    "review": "수동 검토 필요",
    "limited_visibility": "검사 범위 제한",
    "insufficient_evidence": "판정 근거 부족",
    "low_observed_risk": "관찰된 위험 낮음",
}
FINDING_LABELS = {
    "hidden_unicode": "도구 정의에 보이지 않는 서식 문자가 있습니다",
    "credential_exfil_instruction": "민감 파일을 읽어 다른 곳에 포함·전송하라는 지시가 있습니다",
    "role_override": "도구 정의에 상위 지시를 무시하거나 역할을 바꾸라는 문구가 있습니다",
    "privileged_tool": "명령 실행·삭제·쓰기 기능을 암시하는 도구 이름입니다",
    "filesystem_tool": "파일시스템 접근을 암시하는 도구 이름입니다",
    "network_tool": "외부 통신을 암시하는 도구 이름입니다",
    "secret_tool": "자격증명 접근을 암시하는 도구 이름입니다",
    "contradictory_annotation": "읽기 전용이라는 자기 신고가 도구 이름의 동작과 충돌합니다",
    "permissive_schema": "고권한 도구가 선언되지 않은 입력 필드도 허용합니다",
    "sensitive_path": "소스에 민감한 로컬 경로 참조가 있습니다",
    "environment_access": "소스가 환경변수를 읽습니다. 비밀값 접근 가능성을 검토해야 합니다",
    "command_execution": "소스가 별도 명령을 실행할 수 있습니다",
    "outbound_network": "소스가 외부 요청을 보낼 수 있습니다",
    "filesystem_code": "소스가 파일을 읽거나 쓸 수 있습니다",
    "install_script": "패키지 설치 과정에서 스크립트가 실행됩니다",
    "floating_dependency": "의존성 버전이 정확히 고정되지 않았습니다",
    "missing_lockfile": "의존성 잠금 파일을 찾지 못했습니다",
    "broad_scope": "수정·관리 권한을 줄 수 있는 넓은 인가 범위입니다",
    "definition_changed": "도구 정의가 저장된 기준 지문과 달라졌습니다",
    "sensitive_argument": "도구 인자에서 민감 경로나 자격증명 형태의 값을 관찰했습니다",
    "poisoned_result": "도구 응답에서 공격 지시 또는 자격증명 형태의 값을 관찰했습니다",
    "catalog_changed": "도구 목록이 저장된 기준 지문과 달라졌습니다",
    "uninspectable_result": "도구 응답 일부를 검사하지 못했습니다",
}


def _safe(value: object) -> str:
    """Render untrusted names/evidence without terminal control characters."""
    return "".join(char if char.isprintable() and char not in {"\u202e", "\u202d"}
                   else f"\\u{ord(char):04x}" for char in str(value))


def render_text(report: Report, changes: list[dict[str, str]] | None = None) -> str:
    lines = [
        f"MCP 서버: {_safe(report.server_id)}",
        f"Security Score: {report.security_score}/100 (Risk Score: {report.risk_score}/100; 관찰된 신호 기준)",
        f"판정: {VERDICT_LABELS.get(report.verdict, report.verdict)} ({report.verdict})",
        "",
        "위험 항목 (감점/최대):",
    ]
    for category, maximum in CAPS.items():
        lines.append(f"  {LABELS[category]}: {report.penalties[category]}/{maximum}")
    lines.extend(["", "검사 범위:"])
    for name, state in report.coverage.items():
        lines.append(f"  {name}: {state}")
    lines.extend(["", "위험 근거:"])
    if report.findings:
        for finding in report.findings:
            message = FINDING_LABELS.get(finding.code, finding.message)
            lines.append(f"  [{finding.severity.upper()}] {_safe(message)} "
                         f"({_safe(finding.evidence)}; {finding.category} +{finding.points}, {finding.confidence})")
    else:
        lines.append("  관찰된 위험 신호 없음 — 안전 인증은 아닙니다.")
    if changes:
        lines.extend(["", "기준 지문과의 차이:"])
        for change in changes:
            lines.append(f"  {_safe(change['tool'])}: {_safe(change['change'])}")
    if report.llm_suggestions:
        lines.extend(["", "LLM 검토 제안 (미검증, 점수 미반영):"])
        for suggestion in report.llm_suggestions:
            lines.append(f"  [{suggestion['severity'].upper()}] {_safe(suggestion['tool'])}."
                         f"{_safe(suggestion['field'])}: {_safe(suggestion['reason'])}")
    lines.extend(["", "참고: 검사되지 않은 영역은 0점 위험으로 증명된 것이 아닙니다."])
    return "\n".join(lines)
