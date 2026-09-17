/**
 * background/config.js
 *
 * 서버 선택 상수 (PLAN §3, §11).
 *  - 로컬 우선: 48200~48209 포트를 병렬 스캔(각 500ms 타임아웃)해
 *    `service: "campfire"` 시그니처가 일치하는 첫 포트를 채택.
 *  - 전부 실패하면 **전송을 막는다**. 원격 폴백은 제거했다(아래).
 */

// 원격 폴백(https://api.airookieupsecurity.com)은 제거했다.
//
// 그 경로로 나가던 것은 사용자의 프롬프트 전문과 첨부 파일 바이트 전체였고, 인증도
// 없었다("원격 인증 사양 없음"). 데스크탑 앱이 꺼져 있기만 하면 로컬 우선 마스킹
// 게이트웨이가 조용히 제3자 업로더가 된다 — 검토 패널엔 표시도 없었다.
// 마스킹을 못 하는 상황이면 보내지 않는 쪽이 맞다.
// (service-worker.js discoverServer 가 이제 예외를 던진다)

// 로컬 포트 스캔 범위 (PLAN §3/§11)
export const LOCAL_HOST = '127.0.0.1';
export const BASE_PORT = 48200;
export const PORT_SCAN_COUNT = 10;               // 48200~48209
export const HEALTH_TIMEOUT_MS = 500;            // 포트별 /health 타임아웃

// 엔진 식별 시그니처 (engine app/config.py SERVICE_NAME 과 동일해야 함)
const SERVICE_SIGNATURE = 'campfire';

// 리브랜딩(UpSecurity/securedoc-gateway -> Campfire) 이전 엔진이 내보내던 시그니처.
// 확장은 앱 설치본에 함께 들어가지만 Chrome 이 로드한 사본은 사용자가 직접 새로고침해야
// 갱신되므로, 새 확장이 아직 안 바뀐 엔진과 만나는 구간이 생긴다. 그때 연결이 끊기지
// 않도록 옛 시그니처도 받아준다(엔진이 전부 새 버전이 되면 지워도 된다).
const LEGACY_SERVICE_SIGNATURES = ['securedoc-gateway'];
export const isOurEngine = (service) =>
  service === SERVICE_SIGNATURE || LEGACY_SERVICE_SIGNATURES.includes(service);

// chrome.storage.session 캐시 키
export const CACHE_KEY = 'securedocServer';
