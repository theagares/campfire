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

function fixture(detail, answers = [0]) {
  const posts = [];
  const shown = [];
  const fetchImpl = async (url, options = {}) => {
    if (options.method === 'POST') {
      posts.push(JSON.parse(options.body));
      return response({ ok: true });
    }
    if (url.endsWith('/decisions')) {
      return response({ pending: [{ id: detail.id, fileName: detail.fileName, host: detail.host }] });
    }
    return response(detail);
  };
  const ui = new ProxyDecisionController({
    engineManager: manager(), fetchImpl,
    dialog: { showMessageBox: async (opts) => { shown.push(opts); return { response: answers.shift() ?? 0 }; } },
  });
  return { ui, posts, shown };
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

test('일반 탐지는 마스킹 전송을 기본값으로 하고 원본은 재확인한다', async () => {
  const { ui, posts, shown } = fixture({
    id: 'd1', fileName: 'secret.docx', host: 'claude.ai',
    piiCount: 2, injectionCount: 1, forcedMaskCount: 0,
    scanStatus: 'ok', result: { scanStatus: 'ok' },
  });
  await ui.checkNow();
  assert.deepEqual(shown[0].buttons, ['마스킹본 전송', '취소', '원본 전송']);
  assert.equal(shown[0].defaultId, 0);
  assert.deepEqual(posts, [{ action: 'send_masked' }]);
});

test('사용자 지정 마스킹이 있으면 원본 전송 선택지를 만들지 않는다', async () => {
  const { ui, posts, shown } = fixture({
    id: 'd2', fileName: 'secret.txt', host: 'chatgpt.com',
    piiCount: 0, injectionCount: 0, forcedMaskCount: 1,
    scanStatus: 'ok', result: {
      scanStatus: 'ok',
      forcedMaskItems: [{ start: 0, end: 6, type: 'USER_DEFINED_TERM', mandatory: true }],
    },
  }, [1]);
  await ui.checkNow();
  assert.deepEqual(shown[0].buttons, ['마스킹본 전송', '취소']);
  assert.equal(shown.length, 1, '원본 재확인 창도 없어야 한다');
  assert.deepEqual(posts, [{ action: 'cancel' }]);
});

test('검사 미완료 결과는 취소를 기본값으로 둔다', async () => {
  const { ui, posts, shown } = fixture({
    id: 'd3', fileName: 'broken.pdf', host: 'claude.ai',
    piiCount: 0, injectionCount: 0, forcedMaskCount: 0,
    scanStatus: 'failed', result: { reason: '파싱 실패' },
  });
  await ui.checkNow();
  assert.deepEqual(shown[0].buttons, ['취소', '원본 전송']);
  assert.equal(shown[0].defaultId, 0);
  assert.deepEqual(posts, [{ action: 'cancel' }]);
});
