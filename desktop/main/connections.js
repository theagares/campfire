'use strict';
/**
 * main/connections.js
 * "연결" 화면(PLAN §8) 상태 판정 — 판정 기준은 "활성 세션".
 *
 * 한계 (정직히 기록): 현재 엔진 REST 계약(/health, /jobs, /jobs/prompt, /jobs/{id}/events)에는
 *   - 활성 MCP 세션 목록
 *   - 익스텐션 background 활성 연결
 * 을 조회하는 엔드포인트가 없다. 엔진 수정은 금지이므로, 앱은 관측 가능한 신호만으로
 * best-effort 판정한다:
 *   - MCP: 엔진이 실행 중이면 /mcp 엔드포인트가 마운트되어 "연결 가능" 상태. 다만 실제
 *          활성 세션 유무는 엔진에 sessions 조회 API 가 추가돼야 정확히 알 수 있다(gap).
 *   - Extension: 앱에서 직접 관측 불가. 설치 안내만 제공하고 활성 연결은 unknown.
 * → UI 는 이 한계를 그대로 표기한다(허위 "연결됨" 금지).
 */

const http = require('http');
const constants = require('./constants');

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

/**
 * @param {ReturnType<import('./engine-manager').EngineManager['getStatus']>} engineStatus
 */
async function getConnections(engineStatus) {
  const running = engineStatus.state === 'running' && !!engineStatus.port;
  const mcpMounted = running ? await probeMcpMounted(engineStatus.port) : false;

  return {
    mcp: {
      // 활성 세션 판정은 엔진 API 확장 필요(gap). 지금은 "엔진 가동=연결 가능"까지만 확정.
      status: running && mcpMounted ? 'available' : 'unavailable',
      activeSession: null, // 알 수 없음 (엔진에 sessions 조회 API 없음)
      note: running
        ? '엔진 실행 중 — MCP 엔드포인트 이용 가능. (활성 세션 정확 판정은 엔진 sessions API 필요)'
        : '엔진이 실행 중이 아닙니다',
      command: 'npx upsecurity-mcp connect', // PLAN §8: 1줄만 확정, 나머지는 Figma 플레이스홀더
    },
    extension: {
      // 앱에서 익스텐션 활성 연결을 직접 관측할 수 없음 → unknown
      status: 'unknown',
      activeConnection: null,
      note: '익스텐션 활성 연결은 앱에서 직접 관측할 수 없습니다. 설치 후 브라우저에서 확인하세요.',
      helpUrl: constants.EXTENSION_HELP_URL,
    },
  };
}

module.exports = { getConnections };
