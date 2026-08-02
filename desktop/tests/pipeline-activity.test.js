'use strict';
/**
 * main/pipeline-activity.js 회귀 테스트 (node --test, 의존성 없음).
 *
 * 왜 필요한가: 이 모듈이 엔진 상태를 잘못 읽으면 "조용히 아무 일도 안 일어난다".
 * 실제로 처음 구현할 때 engineManager.getStatus().running 을 봤는데 그런 필드가
 * 없어서(state 문자열만 있다) 한 번도 연결되지 않았고, 배선은 전부 멀쩡해 보여서
 * 원인을 찾는 데 오래 걸렸다. 그 계약을 여기에 못 박는다.
 */

const test = require('node:test');
const assert = require('node:assert');
const http = require('node:http');

const { PipelineActivity } = require('../main/pipeline-activity');

/** SSE 를 흘려주는 최소 엔진 스텁. */
function startFakeEngine() {
  let hits = 0;
  let res = null;
  const server = http.createServer((req, r) => {
    if (req.url !== '/activity/stream') { r.writeHead(404).end(); return; }
    hits += 1;
    res = r;
    r.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' });
    r.write('data: {"type":"snapshot","active":[]}\n\n');
  });
  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => {
      resolve({
        baseUrl: `http://127.0.0.1:${server.address().port}`,
        get hits() { return hits; },
        push(obj) { if (res) res.write(`data: ${JSON.stringify(obj)}\n\n`); },
        close() { try { res && res.end(); } catch {} server.close(); },
      });
    });
  });
}

function fakeManager(status) {
  return { getStatus: () => status, on: () => {} };
}

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

test('state 가 running 이면 구독하고 이벤트를 파싱한다', async () => {
  const engine = await startFakeEngine();
  const activity = new PipelineActivity(fakeManager({ state: 'running', baseUrl: engine.baseUrl }));
  const got = [];
  activity.on('activity', (ev) => got.push(ev));
  activity.start();
  try {
    await wait(300);
    assert.equal(engine.hits, 1, '엔진에 SSE 연결이 한 번 이뤄져야 한다');
    assert.equal(got[0].type, 'snapshot', '첫 프레임은 스냅샷');

    engine.push({ type: 'activity', phase: 'progress', stage: 'pii', jobId: 'j1' });
    await wait(200);
    assert.equal(got.length, 2);
    assert.equal(got[1].stage, 'pii');
  } finally {
    activity.stop();
    engine.close();
  }
});

test('running 이 아니면 연결하지 않는다 (state 를 안 보고 running 필드를 보던 회귀 방지)', async () => {
  const engine = await startFakeEngine();
  const activity = new PipelineActivity(fakeManager({ state: 'starting', baseUrl: engine.baseUrl }));
  activity.start();
  try {
    await wait(300);
    assert.equal(engine.hits, 0, '기동 중인 엔진에는 붙지 않아야 한다');
  } finally {
    activity.stop();
    engine.close();
  }
});

test('baseUrl 이 없으면 연결하지 않는다', async () => {
  const engine = await startFakeEngine();
  const activity = new PipelineActivity(fakeManager({ state: 'running', baseUrl: null }));
  activity.start();
  try {
    await wait(300);
    assert.equal(engine.hits, 0);
  } finally {
    activity.stop();
    engine.close();
  }
});

test('여러 프레임이 한 청크로 뭉쳐 와도 각각 파싱한다', async () => {
  const engine = await startFakeEngine();
  const activity = new PipelineActivity(fakeManager({ state: 'running', baseUrl: engine.baseUrl }));
  const got = [];
  activity.on('activity', (ev) => got.push(ev));
  activity.start();
  try {
    await wait(300);
    got.length = 0;
    // keepalive 주석 + 프레임 2개를 한 번에
    engine.push({ type: 'activity', phase: 'progress', stage: 'parse', jobId: 'j1' });
    engine.push({ type: 'activity', phase: 'finish', stage: 'done', jobId: 'j1' });
    await wait(250);
    assert.deepEqual(got.map((e) => e.stage), ['parse', 'done']);
  } finally {
    activity.stop();
    engine.close();
  }
});

test('stop() 후에는 재연결 타이머가 남지 않는다', async () => {
  const engine = await startFakeEngine();
  const activity = new PipelineActivity(fakeManager({ state: 'running', baseUrl: engine.baseUrl }));
  activity.start();
  await wait(200);
  activity.stop();
  const before = engine.hits;
  engine.close();
  await wait(400);
  assert.equal(engine.hits, before, 'stop() 이후 추가 연결 시도가 없어야 한다');
});
