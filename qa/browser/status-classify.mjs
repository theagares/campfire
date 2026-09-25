// qa/browser/status-classify.mjs
// 라이브-QA 사이트 상태의 CI 판정.
//
// 이 스위트의 하드 실패는 **보안 회귀뿐**이다 — 원본 바이트가 나간 경우(failed-leak).
// 그 외 자동화로는 확인이 안 되는 상태(봇 인증·지역 차단·업로드 UI 없음)와, 확장이
// fail-closed 로 안전하게 막은 상태(attachment-reinject-failed 등)는 회귀가 아니라
// "자동 검증 불가 → 수동 확인 대상" 이다. 이걸 CI 실패로 두면 chatgpt·copilot·claude 가
// 실사용에선 멀쩡한데도 CI 를 상시 빨갛게 만든다(orca 로 claude 첨부가 실브라우저에선
// 정상임을 확인). 그래서 경고로 내리고 CI 는 안 깬다 — 단, 유출은 언제나 하드 실패.

export const VERIFIED_STATUSES = new Set(['upload-response-ok', 'content-response-ok']);
export const LEAK_STATUSES = new Set(['failed-leak']); // 원본 유출 = 진짜 회귀, 항상 하드 실패
export const BLOCKED_STATUSES = new Set([              // 자동 검증 불가 / fail-closed(안전) — 경고
  'human-verification-needed', 'region-blocked', 'upload-control-needed',
  'provider-upload-limit', 'attachment-reinject-failed',
]);
// preflight 에서 외부 사유로 못 보는 것(우리 셀렉터 문제가 아님)
export const PREFLIGHT_BLOCKED = new Set(['human-verification-needed', 'region-blocked', 'upload-control-needed']);

/** CI 하드 실패인가. 유출·(우리 탓인) 미분류 실패만 참. blocked/verified 는 거짓. */
export function isHardFail(command, status) {
  if (LEAK_STATUSES.has(status)) return true;                 // 유출은 무조건
  if (command === 'preflight') return status !== 'ready' && !PREFLIGHT_BLOCKED.has(status);
  // run: 검증됨/차단(경고) 외에는 예상 못 한 실패 — login-or-selector-needed·
  // inconclusive 등 우리 쪽 문제일 수 있으므로 하드 실패로 남긴다.
  return !VERIFIED_STATUSES.has(status) && !BLOCKED_STATUSES.has(status);
}

/** 요약 표시용 라벨. */
export function statusMark(command, status) {
  if (LEAK_STATUSES.has(status)) return 'LEAK-FAIL';
  if (command === 'preflight' ? status === 'ready' : VERIFIED_STATUSES.has(status)) return 'ok';
  if (isHardFail(command, status)) return 'FAIL';
  return 'blocked(manual)';
}
