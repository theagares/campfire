'use strict';
/**
 * proxy-toggle.js 테스트 (node --test, 의존성 없음).
 *
 * 이 모듈은 사용자의 **시스템 프록시 설정**을 바꾼다. 틀리면 사용자 브라우저가 통째로
 * 막히거나, 반대로 검사 없이 새어 나간다. 그래서 실제 설정·엔진·PAC 서버는 전부
 * 가짜로 갈아끼우고 순서와 되돌리기만 본다.
 *
 * 지키려는 것:
 *  1) 켜기는 엔진이 실제로 듣고 CA 가 신뢰될 때만 시스템을 바꾼다.
 *  2) 끄기는 시스템부터 돌려놓는다(엔진 먼저 끄면 브라우저가 죽은 포트를 향한다).
 *  3) 원래 설정은 시스템을 바꾸기 **전에** 저장된다. 강제 종료 후 되돌릴 수 있어야 한다.
 *  4) 이미 다른 프록시가 있으면 손대지 않는다.
 *  5) 되돌리기는 원래 값 그대로다 — 우리 PAC 를 "원래 값" 으로 기억하지 않는다.
 */

const test = require('node:test');
const assert = require('node:assert');

const systemProxyMod = require('../main/system-proxy');
const { create } = require('../main/proxy-toggle');

const PAC_PREFIX = 'http://127.0.0.1:48211/campfire.pac';
const ORIGINAL = { flags: 9, server: null, bypass: '127.0.0.1:16107', pacUrl: null };

function harness({
  engineStatus = { running: true, caTrusted: true, port: 48210,
    caPath: 'C:\\ca.cer', hosts: { exact: ['claude.ai'], suffixes: [] } },
  system = { ...ORIGINAL },
  engineUp = true,
} = {}) {
  const log = [];
  const store = { proxyEnabled: false, proxySystemPrevious: null };
  const config = {
    get: (k) => store[k],
    set: (patch) => { log.push(['config', { ...patch }]); Object.assign(store, patch); },
  };

  let sys = { ...system };
  const runner = async (cmd, arg) => {
    log.push(['system', cmd]);
    if (cmd === 'query') return { ...sys };
    if (cmd === 'set-pac') { sys = { ...sys, flags: 5, pacUrl: arg }; return { ...sys }; }
    if (cmd === 'restore') { sys = { ...JSON.parse(arg) }; return { ...sys }; }
    throw new Error(cmd);
  };
  const systemProxy = systemProxyMod.create(runner);

  let serving = false;
  const pacServer = {
    PAC_PREFIX,
    isRunning: () => serving,
    publish: async () => { log.push(['pac', 'publish']); serving = true; return `${PAC_PREFIX}?v=1`; },
    stop: async () => { log.push(['pac', 'stop']); serving = false; },
  };

  const engineManager = { getStatus: () => ({ baseUrl: engineUp ? 'http://127.0.0.1:48200' : null }) };
  const fetchImpl = async (url, { method }) => {
    const p = url.replace('http://127.0.0.1:48200', '');
    log.push(['engine', `${method} ${p}`]);
    return { ok: true, json: async () => (p === '/proxy/stop' ? { ...engineStatus, running: false } : engineStatus) };
  };

  const toggle = create({ config, engineManager, systemProxy, pacServer, fetchImpl });
  return { toggle, log, store, getSystem: () => sys };
}

const steps = (log, kind) => log.filter((e) => e[0] === kind).map((e) => e[1]);
const order = (log) => log.map((e) => (e[0] === 'config' ? `config:${Object.keys(e[1]).join(',')}` : `${e[0]}:${e[1]}`));

test('켜기: 엔진 → PAC → 원래값 저장 → 시스템 순서', async () => {
  const h = harness();
  const r = await h.toggle.set(true);
  assert.strictEqual(r.ok, true);

  const seq = order(h.log);
  const i = (s) => seq.indexOf(s);
  assert.ok(i('engine:POST /proxy/start') < i('pac:publish'));
  // 원래값을 디스크에 쓴 **뒤에** 시스템을 바꾼다 — 그 사이에 죽어도 되돌릴 수 있다.
  assert.ok(i('config:proxySystemPrevious') < i('system:set-pac'), seq.join(' → '));
  assert.deepStrictEqual(h.store.proxySystemPrevious, ORIGINAL);
  assert.strictEqual(h.store.proxyEnabled, true);
  assert.ok(h.getSystem().pacUrl.startsWith(PAC_PREFIX));
});

test('끄기: 시스템을 먼저 돌려놓고 엔진은 마지막', async () => {
  const h = harness();
  await h.toggle.set(true);
  h.log.length = 0;

  const r = await h.toggle.set(false);
  assert.strictEqual(r.ok, true);
  const seq = order(h.log);
  assert.ok(seq.indexOf('system:restore') < seq.indexOf('engine:POST /proxy/stop'), seq.join(' → '));
  assert.ok(seq.indexOf('system:restore') < seq.indexOf('pac:stop'));

  // 원래 값 그대로 — 자동 검색(9)과 기존 예외 목록까지.
  assert.deepStrictEqual(h.getSystem(), ORIGINAL);
  assert.strictEqual(h.store.proxySystemPrevious, null);
  assert.strictEqual(h.store.proxyEnabled, false);
});

test('CA 가 신뢰되지 않으면 시스템을 건드리지 않는다', async () => {
  const h = harness({ engineStatus: { running: true, caTrusted: false, port: 48210, caPath: 'C:\\ca.cer', hosts: { exact: [], suffixes: [] } } });
  const r = await h.toggle.set(true);
  assert.strictEqual(r.ok, false);
  assert.strictEqual(r.code, 'ca');
  assert.strictEqual(r.caPath, 'C:\\ca.cer');
  assert.ok(!steps(h.log, 'system').includes('set-pac'), 'CA 없이 브라우저를 돌렸다 — AI 사이트가 전부 인증서 오류');
  assert.ok(steps(h.log, 'engine').includes('POST /proxy/stop'), '아무도 안 오는 프록시를 켜 둔다');
  assert.strictEqual(h.store.proxyEnabled, false);
});

test('엔진 프록시가 포트를 못 잡으면 시스템을 건드리지 않는다', async () => {
  const h = harness({ engineStatus: { running: false, caTrusted: true, port: 48210, hosts: { exact: [], suffixes: [] } } });
  const r = await h.toggle.set(true);
  assert.strictEqual(r.ok, false);
  assert.strictEqual(r.code, 'engine');
  assert.ok(!steps(h.log, 'system').includes('set-pac'), '죽은 포트로 브라우저를 돌렸다');
});

test('다른 프록시가 이미 있으면 손대지 않는다', async () => {
  const corp = { flags: 3, server: 'proxy.corp:8080', bypass: '<local>', pacUrl: null };
  const h = harness({ system: corp });
  const r = await h.toggle.set(true);
  assert.strictEqual(r.ok, false);
  assert.strictEqual(r.code, 'conflict');
  assert.deepStrictEqual(h.getSystem(), corp, '회사 프록시를 덮었다');
  assert.strictEqual(h.store.proxySystemPrevious, null);
});

test('다시 켜도 원래 값은 처음 것 그대로', async () => {
  // 엔진이 떴을 때 resume 이 다시 켜는 경우. 두 번째 켜기가 "지금 설정(=우리 PAC)" 을
  // 원래 값으로 저장하면, 끌 때 우리 PAC 로 "되돌려" 영영 못 풀린다.
  const h = harness();
  await h.toggle.set(true);
  await h.toggle.set(true);
  assert.deepStrictEqual(h.store.proxySystemPrevious, ORIGINAL);
  await h.toggle.set(false);
  assert.deepStrictEqual(h.getSystem(), ORIGINAL);
});

test('강제 종료 뒤 다음 실행: 엔진보다 먼저 되돌린다', async () => {
  // 지난 실행이 켜 둔 채 죽었다: 설정은 우리 PAC, 되돌릴 값은 디스크에.
  const h = harness({ system: { flags: 5, server: null, bypass: '127.0.0.1:16107', pacUrl: `${PAC_PREFIX}?v=1` } });
  h.store.proxySystemPrevious = { ...ORIGINAL };
  h.store.proxyEnabled = true;

  await h.toggle.recoverOnLaunch();
  assert.deepStrictEqual(h.getSystem(), ORIGINAL, 'PAC 서버도 없는데 브라우저가 계속 그걸 찾는다');
  assert.strictEqual(h.store.proxySystemPrevious, null);
  assert.strictEqual(h.store.proxyEnabled, true, '사용자 선택은 기억한다 — 엔진이 뜨면 다시 켠다');
});

test('앱 종료(suspend): 시스템은 돌려놓되 켜짐은 기억한다', async () => {
  const h = harness();
  await h.toggle.set(true);
  await h.toggle.suspend();
  assert.deepStrictEqual(h.getSystem(), ORIGINAL);
  assert.strictEqual(h.store.proxyEnabled, true);
});

test('resumeIfDesired: 원할 때만, 이미 걸려 있으면 다시 안 건다', async () => {
  const off = harness();
  assert.strictEqual(await off.toggle.resumeIfDesired(), null);
  assert.ok(!steps(off.log, 'engine').includes('POST /proxy/start'));

  const on = harness();
  on.store.proxyEnabled = true;
  const r = await on.toggle.resumeIfDesired();
  assert.strictEqual(r.ok, true);
  on.log.length = 0;
  await on.toggle.resumeIfDesired(); // 엔진 재시작으로 다시 불려도
  assert.deepStrictEqual(steps(on.log, 'system'), [], '이미 걸린 걸 또 건드렸다');
});

test('켜기·끄기가 겹쳐도 한 줄로 돈다', async () => {
  const h = harness();
  await Promise.all([h.toggle.set(true), h.toggle.set(false), h.toggle.set(true)]);
  // 마지막 요청이 이긴다. 중간에 원래값을 우리 PAC 로 덮지 않는다.
  assert.strictEqual(h.store.proxyEnabled, true);
  assert.deepStrictEqual(h.store.proxySystemPrevious, ORIGINAL);
});
