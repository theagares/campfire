'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { ProxyDecisionController } = require('../main/proxy-decision-ui');

function response(body, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

function manager() {
  return { getStatus: () => ({ state: 'running', baseUrl: 'http://127.0.0.1:48200' }) };
}

test('대기 중인 판단이 없으면 확인창을 띄우지 않는다', async () => {
  let dialogs = 0;
  const ui = new ProxyDecisionController({
    engineManager: manager(),
    dialog: { showMessageBox: async () => { dialogs += 1; return { response: 0 }; } },
    fetchImpl: async () => response({ pending: [] }),
  });
  assert.equal(await ui.checkNow(), false);
  assert.equal(dialogs, 0);
});

test('탐지 결과에서 기본 선택은 마스킹본 전송이다', async () => {
  const calls = [];
  const posts = [];
  const fetchImpl = async (url, options = {}) => {
    calls.push({ url, options });
    if (options.method === 'POST') {
      posts.push(JSON.parse(options.body));
      return response({ ok: true });
    }
    if (url.endsWith('/decisions')) {
      return response({ pending: [{ id: 'd1', fileName: 'secret.docx', host: 'claude.ai' }] });
    }
    return response({
      id: 'd1', fileName: 'secret.docx', host: 'claude.ai',
      piiCount: 2, injectionCount: 1, scanStatus: 'ok', result: { scanStatus: 'ok' },
    });
  };
  const shown = [];
  const ui = new ProxyDecisionController({
    engineManager: manager(), fetchImpl,
    dialog: { showMessageBox: async (opts) => { shown.push(opts); return { response: 0 }; } },
  });

  assert.equal(await ui.checkNow(), true);
  assert.equal(shown.length, 1);
  assert.deepEqual(shown[0].buttons, ['마스킹본 전송', '취소', '원본 전송']);
  assert.equal(shown[0].defaultId, 0);
  assert.deepEqual(posts, [{ action: 'send_masked' }]);
  assert.ok(calls.some((c) => c.url.endsWith('/decisions/d1')));
});

test('원본 전송은 두 번째 경고에서 다시 확인한다', async () => {
  const posts = [];
  const fetchImpl = async (url, options = {}) => {
    if (options.method === 'POST') {
      posts.push(JSON.parse(options.body));
      return response({ ok: true });
    }
    if (url.endsWith('/decisions')) {
      return response({ pending: [{ id: 'd2', fileName: 'a.txt', host: 'chatgpt.com' }] });
    }
    return response({
      id: 'd2', fileName: 'a.txt', host: 'chatgpt.com',
      piiCount: 1, injectionCount: 0, scanStatus: 'ok', result: { scanStatus: 'ok' },
    });
  };
  const answers = [2, 0]; // 원본 전송을 눌렀지만 두 번째 경고에서 취소
  const ui = new ProxyDecisionController({
    engineManager: manager(), fetchImpl,
    dialog: { showMessageBox: async () => ({ response: answers.shift() }) },
  });

  await ui.checkNow();
  assert.deepEqual(posts, [{ action: 'cancel' }]);
});

test('검사 미완료 결과는 취소를 기본값으로 둔다', async () => {
  const posts = [];
  const fetchImpl = async (url, options = {}) => {
    if (options.method === 'POST') {
      posts.push(JSON.parse(options.body));
      return response({ ok: true });
    }
    if (url.endsWith('/decisions')) {
      return response({ pending: [{ id: 'd3', fileName: 'broken.pdf', host: 'claude.ai' }] });
    }
    return response({
      id: 'd3', fileName: 'broken.pdf', host: 'claude.ai', piiCount: 0,
      injectionCount: 0, scanStatus: 'failed', result: { reason: '파싱 실패' },
    });
  };
  let options;
  const ui = new ProxyDecisionController({
    engineManager: manager(), fetchImpl,
    dialog: { showMessageBox: async (opts) => { options = opts; return { response: 0 }; } },
  });

  await ui.checkNow();
  assert.deepEqual(options.buttons, ['취소', '원본 전송']);
  assert.equal(options.defaultId, 0);
  assert.deepEqual(posts, [{ action: 'cancel' }]);
});
