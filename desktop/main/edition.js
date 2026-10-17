'use strict';
/**
 * main/edition.js
 * 라이트 판 — 버전에 -lite 가 붙은 빌드(예: 0.2.34-lite.1). PII·인젝션·사용자 지정 강제 마스킹만 쓰고
 * 기밀 탐지·프록시·MCP(연결·위험 검사기)·시연 대시보드는 끈다. 자동 업데이트도 -lite 릴리스만 받는다
 * (electron-updater 는 alpha·beta 가 아닌 채널을 같은 채널끼리만 잇는다). 엔진은 SECUREDOC_EDITION=lite 로
 * 같은 기능을 다시 막는다 — 화면만 숨기지 않는다.
 *
 * 개발 중 확인용: CAMPFIRE_EDITION=lite 로 띄우면 버전과 상관없이 라이트로 동작한다.
 */

// 라이트 판에서 저장을 막는 설정. 정식 판을 쓰다 라이트로 바꿔 설치해도 이 값들이 켜진 채 남아 있을 수 있어
// 엔진(SECUREDOC_EDITION=lite)이 무시하지만, 화면 밖에서 다시 켜는 경로도 막아 둔다.
const LITE_LOCKED_SETTINGS = ['confidentialEnabled', 'kgPath', 'mcpRiskScannerEnabled', 'proxyEnabled'];

function isLite(version, env = process.env) {
  return env.CAMPFIRE_EDITION === 'lite' || /-lite(\.|$)/.test(String(version || ''));
}

function liteSettingsPatch(patch) {
  const out = { ...(patch || {}) };
  for (const key of LITE_LOCKED_SETTINGS) delete out[key];
  return out;
}

module.exports = { isLite, liteSettingsPatch, LITE_LOCKED_SETTINGS };
