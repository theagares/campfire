/**
 * background/config.js
 *
 * 서버 선택 상수 (PLAN §3, §11).
 *  - 로컬 우선: 48200~48209 포트를 병렬 스캔(각 500ms 타임아웃)해
 *    `service: "securedoc-gateway"` 시그니처가 일치하는 첫 포트를 채택.
 *  - 전부 실패 시 원격 URL 폴백(기본값 아래). 원격 URL은 익스텐션에서 편집하지
 *    않는다(PLAN §변경2: 원격 URL은 앱 설정에만 존재).
 */

// 원격 폴백 (읽기 전용 — 익스텐션에선 수정 불가)
export const REMOTE_URL = 'https://api.airookieupsecurity.com';
// 원격 인증 토큰은 배포되는 익스텐션 소스에 절대 하드코딩하지 않는다.
// v1 계획(PLAN §3)엔 원격 인증 사양이 없다. 향후 원격 API에 인증이 필요해지면
// 빌드타임 시크릿 주입 또는 사용자 설정(런타임)에서 읽어오도록 하고, 이 파일엔
// 값을 넣지 않는다.

// 로컬 포트 스캔 범위 (PLAN §3/§11)
export const LOCAL_HOST = '127.0.0.1';
export const BASE_PORT = 48200;
export const PORT_SCAN_COUNT = 10;               // 48200~48209
export const HEALTH_TIMEOUT_MS = 500;            // 포트별 /health 타임아웃

// 엔진 식별 시그니처 (engine app/config.py SERVICE_NAME 과 동일해야 함)
export const SERVICE_SIGNATURE = 'securedoc-gateway';

// chrome.storage.session 캐시 키
export const CACHE_KEY = 'securedocServer';
