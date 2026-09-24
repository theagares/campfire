'use strict';
/**
 * pac-server.js 테스트 — PAC 가 AI 사이트만 프록시로 보내는가.
 *
 * PAC 는 브라우저 안에서 도는 JS 라 문자열로만 확인하면 놓친다. vm 에 dnsDomainIs 를
 * 넣고 실제로 FindProxyForURL 을 불러 본다.
 */

const test = require('node:test');
const assert = require('node:assert');
const vm = require('node:vm');
const http = require('node:http');

const pac = require('../main/pac-server');

const HOSTS = {
  exact: ['chatgpt.com', 'claude.ai', 'push.clients6.google.com'],
  suffixes: ['oaiusercontent.com'],
};

function load(src) {
  const ctx = {
    // 브라우저 PAC 런타임이 주는 함수. 명세대로 "host 가 domain 으로 끝나는가".
    dnsDomainIs: (host, domain) => host.length >= domain.length
      && host.slice(host.length - domain.length) === domain,
  };
  vm.createContext(ctx);
  vm.runInContext(src, ctx);
  return (host) => ctx.FindProxyForURL(`https://${host}/`, host);
}

test('AI 사이트는 프록시로, 나머지는 직접', () => {
  const find = load(pac.buildPac(HOSTS, 48210));
  assert.strictEqual(find('claude.ai'), 'PROXY 127.0.0.1:48210');
  assert.strictEqual(find('CHATGPT.COM'), 'PROXY 127.0.0.1:48210');
  assert.strictEqual(find('sdmntprcentralus.oaiusercontent.com'), 'PROXY 127.0.0.1:48210');
  assert.strictEqual(find('push.clients6.google.com'), 'PROXY 127.0.0.1:48210');

  assert.strictEqual(find('github.com'), 'DIRECT');
  assert.strictEqual(find('www.google.com'), 'DIRECT');
});

test('흉내 도메인은 프록시로 보내지 않는다', () => {
  const find = load(pac.buildPac(HOSTS, 48210));
  assert.strictEqual(find('evil-claude.ai'), 'DIRECT');
  assert.strictEqual(find('claude.ai.evil.com'), 'DIRECT');
  assert.strictEqual(find('notoaiusercontent.com'), 'DIRECT');
});

test('AI 사이트에 DIRECT 대안을 붙이지 않는다', () => {
  // "PROXY …; DIRECT" 면 프록시가 죽었을 때 브라우저가 조용히 직접 나간다 — 검사 없는 업로드.
  const find = load(pac.buildPac(HOSTS, 48210));
  assert.ok(!find('claude.ai').includes('DIRECT'));
});

test('실제로 서빙하고, 내용이 바뀌면 URL 도 바뀐다', async (t) => {
  if (pac.isRunning()) await pac.stop();
  const url1 = await pac.publish(HOSTS, 48210);
  t.after(() => pac.stop());
  assert.ok(url1.startsWith(pac.PAC_PREFIX));

  const body = await new Promise((resolve, reject) => {
    http.get(url1, (res) => {
      assert.strictEqual(res.headers['content-type'], 'application/x-ns-proxy-autoconfig');
      let b = '';
      res.on('data', (c) => { b += c; });
      res.on('end', () => resolve(b));
    }).on('error', reject);
  });
  assert.ok(body.includes('FindProxyForURL'));

  const url2 = await pac.publish({ ...HOSTS, exact: [...HOSTS.exact, 'grok.com'] }, 48210);
  assert.notStrictEqual(url1, url2, '브라우저가 캐시한 옛 PAC 를 계속 쓴다');

  await pac.stop();
  assert.strictEqual(pac.isRunning(), false);
});
