'use strict';
/**
 * main/constants.js
 * 공용 상수. 비밀값(토큰/키/비밀번호) 절대 포함 금지 — placeholder 또는 공개 기본값만.
 */

module.exports = {
  SERVICE_NAME: 'securedoc-gateway', // 엔진 /health 시그니처 (PLAN §11)
  HOST: '127.0.0.1',
  BASE_PORT: 48200,
  PORT_SCAN_COUNT: 10, // 48200~48209 (PLAN §11)
  HEALTH_TIMEOUT_MS: 500, // 포트당 타임아웃 (PLAN §11)
  HEALTH_POLL_INTERVAL_MS: 2500, // 정상 상태 폴링 주기

  // 원격 URL 기본값 — 비밀 아님(PLAN §3 명시적 허용). 사용자가 설정 팝업에서 변경 가능.
  DEFAULT_REMOTE_URL: 'https://api.airookieupsecurity.com',

  // 인젝션 정책 (PLAN §4/§8). 엔진엔 REST 쓰기 엔드포인트가 없어, 앱이 사이드카 spawn 시
  // SECUREDOC_INJECTION_POLICY 환경변수로 반영한다(정책 변경 = 엔진 재시작).
  INJECTION_POLICY_DEFAULT: 'mask', // 'mask' | 'block'

  // 익스텐션 설치 안내 링크 (placeholder — 배포 후 실제 웹스토어/문서 링크로 교체)
  EXTENSION_HELP_URL: 'https://airookieupsecurity.com/extension',
};
