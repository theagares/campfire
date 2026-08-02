'use strict';
/**
 * main/connections.js
 * "연결" 화면(PLAN §8) 상태 판정 — 판정 기준은 "활성 세션".
 *
 *   - MCP: 엔진이 실행 중이면 /mcp 엔드포인트가 마운트되어 "연결 가능" 상태. 다만 실제
 *          활성 세션 유무는 엔진에 sessions 조회 API 가 추가돼야 정확히 알 수 있다(gap).
 *   - Extension: 확장의 background(service worker)가 /health 를 GET 할 때 브라우저가
 *          cross-origin fetch 에 자동으로 붙이는 Origin 헤더가 "chrome-extension://<id>"
 *          형태다 — 엔진이 이 값을 보고 마지막으로 확장에서 요청이 온 시각을
 *          extensionLastSeenSecondsAgo 로 /health 응답에 실어준다(engine/app/adapters/
 *          http_api/health.py). 이 값이 EXTENSION_ACTIVE_WINDOW_SEC 이내면 "연결됨" —
 *          MCP 와 마찬가지로 "설치돼 있음"이 아니라 "최근에 활성 연결이 확인됨" 기준.
 */

const http = require('http');
const constants = require('./constants');

const EXTENSION_ACTIVE_WINDOW_SEC = 60;

/** /mcp 가 응답하는지(마운트 여부) 가벼운 확인. 세션 유무는 판정하지 않는다. */
function probeMcpMounted(port) {
  return new Promise((resolve) => {
    if (!port) return resolve(false);
    // GET /mcp 는 보통 405/406/400 등을 돌려주지만, "존재"는 확인 가능.
    const req = http.request(
      { host: constants.HOST, port, path: '/mcp', method: 'GET', timeout: 500 },
      (res) => {
        res.resume();
        // 404 이외 응답이면 마운트된 것으로 간주
        resolve(res.statusCode !== 404);
      }
    );
    req.on('timeout', () => { req.destroy(); resolve(false); });
    req.on('error', () => resolve(false));
    req.end();
  });
}

/** /health 전체 응답(JSON) 조회 — extensionLastSeenSecondsAgo 포함. */
function fetchHealth(port) {
  return new Promise((resolve) => {
    if (!port) return resolve(null);
    const req = http.request(
      { host: constants.HOST, port, path: '/health', method: 'GET', timeout: 500 },
      (res) => {
        let body = '';
        res.on('data', (chunk) => { body += chunk; });
        res.on('end', () => {
          try { resolve(JSON.parse(body)); } catch { resolve(null); }
        });
      }
    );
    req.on('timeout', () => { req.destroy(); resolve(null); });
    req.on('error', () => resolve(null));
    req.end();
  });
}

/**
 * @param {ReturnType<import('./engine-manager').EngineManager['getStatus']>} engineStatus
 */
async function getConnections(engineStatus) {
  const running = engineStatus.state === 'running' && !!engineStatus.port;
  const [mcpMounted, health] = running
    ? await Promise.all([probeMcpMounted(engineStatus.port), fetchHealth(engineStatus.port)])
    : [false, null];

  const extAgoSec = health && typeof health.extensionLastSeenSecondsAgo === 'number'
    ? health.extensionLastSeenSecondsAgo
    : null;
  const extActive = extAgoSec !== null && extAgoSec <= EXTENSION_ACTIVE_WINDOW_SEC;

  return {
    mcp: {
      // 활성 세션 판정은 엔진 API 확장 필요(gap). 지금은 "엔진 가동=연결 가능"까지만 확정.
      status: running && mcpMounted ? 'available' : 'unavailable',
      activeSession: null, // 알 수 없음 (엔진에 sessions 조회 API 없음)
      note: running
        ? '엔진 실행 중 — MCP 엔드포인트 이용 가능. (활성 세션 정확 판정은 엔진 sessions API 필요)'
        : '엔진이 실행 중이 아닙니다',
      command: 'npx campfire-mcp connect', // PLAN §8: 1줄만 확정, 나머지는 Figma 플레이스홀더
    },
    extension: {
      status: extActive ? 'available' : 'unavailable',
      activeConnection: extActive,
      note: !running
        ? '엔진이 실행 중이 아닙니다'
        : extActive
          ? `브라우저 확장이 ${Math.round(extAgoSec)}초 전 엔진과 통신했습니다`
          : '최근 확장 프로그램의 활성 연결이 확인되지 않았습니다. 확장을 설치한 브라우저에서 지원 사이트를 열어보세요',
      helpUrl: constants.EXTENSION_HELP_URL,
    },
  };
}

module.exports = { getConnections };
