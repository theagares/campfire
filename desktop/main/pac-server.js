'use strict';
/**
 * main/pac-server.js
 * 브라우저에게 "AI 사이트만 프록시로, 나머지는 직접" 을 알려 주는 PAC 파일을 낸다.
 *
 * 엔진이 아니라 앱이 낸다. 엔진은 재시작·강제 종료가 잦은데, PAC 를 못 받으면
 * 브라우저는 DIRECT 로 물러선다 — 그러면 엔진이 내려간 사이 AI 업로드가 검사 없이
 * 나간다. 앱은 엔진보다 오래 살므로 여기서 내면 엔진이 죽어도 PAC 는 그대로고,
 * AI 사이트는 죽은 프록시로 가서 **막힌다**(fail-closed).
 */

const http = require('http');
const crypto = require('crypto');

const HOST = '127.0.0.1';
const PORT = 48211;
const PATH = '/campfire.pac';

/** 우리 PAC 인지 알아보는 접두사. 버전 쿼리는 뗀다. */
const PAC_PREFIX = `http://${HOST}:${PORT}${PATH}`;

/**
 * @param {{exact:string[], suffixes:string[]}} hosts 엔진 /proxy/status 의 hosts
 * @param {number} proxyPort
 */
function buildPac(hosts, proxyPort) {
  const exact = (hosts.exact || []).map((h) => h.toLowerCase());
  const suffixes = (hosts.suffixes || []).map((s) => s.toLowerCase());
  // DIRECT 로 물러서는 대안을 붙이지 않는다("PROXY …; DIRECT" 가 아니다).
  // 붙이면 프록시가 죽었을 때 브라우저가 조용히 직접 나간다 — 검사를 건너뛴 업로드다.
  const target = `PROXY ${HOST}:${proxyPort}`;
  return [
    '// Campfire — AI 사이트만 검사 프록시로 보낸다. 나머지는 직접 나간다.',
    `var EXACT = ${JSON.stringify(exact)};`,
    `var SUFFIX = ${JSON.stringify(suffixes)};`,
    'function FindProxyForURL(url, host) {',
    '  host = host.toLowerCase();',
    '  for (var i = 0; i < EXACT.length; i++) {',
    `    if (host === EXACT[i]) return "${target}";`,
    '  }',
    '  for (var j = 0; j < SUFFIX.length; j++) {',
    `    if (host === SUFFIX[j] || dnsDomainIs(host, "." + SUFFIX[j])) return "${target}";`,
    '  }',
    '  return "DIRECT";',
    '}',
    '',
  ].join('\n');
}

let server = null;
let body = '';

/**
 * PAC 를 갱신하고 서버가 떠 있게 한다. 브라우저에 넘길 URL 을 돌려준다.
 * 내용이 바뀌면 URL 도 바뀐다 — 브라우저가 캐시한 옛 PAC 를 계속 쓰지 않게.
 */
async function publish(hosts, proxyPort) {
  body = buildPac(hosts, proxyPort);
  if (!server) {
    server = http.createServer((req, res) => {
      if (!req.url || !req.url.startsWith(PATH)) {
        res.writeHead(404).end();
        return;
      }
      res.writeHead(200, {
        'Content-Type': 'application/x-ns-proxy-autoconfig',
        'Cache-Control': 'no-store',
      });
      res.end(body);
    });
    await new Promise((resolve, reject) => {
      server.once('error', reject);
      server.listen(PORT, HOST, () => {
        server.off('error', reject);
        resolve();
      });
    }).catch((err) => {
      server = null;
      throw new Error(`PAC 서버를 ${HOST}:${PORT} 에 띄우지 못했습니다: ${err.message}`);
    });
  }
  const v = crypto.createHash('sha1').update(body).digest('hex').slice(0, 10);
  return `${PAC_PREFIX}?v=${v}`;
}

function stop() {
  return new Promise((resolve) => {
    if (!server) {
      resolve();
      return;
    }
    const s = server;
    server = null;
    s.close(() => resolve());
    // 브라우저가 keep-alive 로 붙잡고 있으면 close 가 끝나지 않는다.
    if (typeof s.closeAllConnections === 'function') s.closeAllConnections();
  });
}

function isRunning() {
  return server !== null;
}

module.exports = { buildPac, publish, stop, isRunning, PAC_PREFIX, PORT };
