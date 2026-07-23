"""
app/onboarding
MCP 우회 방지 온보딩 체크리스트 도구 (PLAN §4.2, §10 Phase 6).

엔진과는 독립적인 모듈 — CLI(`python -m app.onboarding.checklist --dry-run`)로도
단독 실행 가능하다.

안전 수칙(필독, 모든 하위 모듈 공통):
    이 패키지의 어떤 함수도 대상 설정 파일 경로를 하드코딩하지 않는다. 각 모듈이
    정의하는 DEFAULT_* 경로 상수는 "실제 사용 시" 참고용 기본값일 뿐이며, 실제
    쓰기는 항상 common.apply_diff(diff, apply=True) 를 명시적으로 호출했을 때만
    일어난다 — 기본은 어디까지나 dry-run(diff 생성만). 이 리포지토리의 테스트는
    tempfile 기반 임시 디렉토리만 대상 경로로 사용하며, 실제 홈 디렉토리
    (~/.claude/ 등)는 어떤 테스트에서도 사용하지 않는다.
"""
