'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { ProxyDecisionController } = require('../main/proxy-decision-ui');

function response(body, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

function manager() {
  return { getStatus: () => ({ state: 'running', baseUrl: 'http://127.0.0.1:48200' }) };
}

function fixture(detail, answers = [0], opts = {}) {
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
    dialog: { showMessageBox: async (o) => { shown.push(o); return { response: answers.shift() ?? 0 }; } },
    ...opts,
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

test('start는 빈 목록을 0.5초마다 폴링하지 않고 목록 버전 변경을 장기 대기한다', async () => {
  const engineManager = new EventEmitter();
  engineManager.getStatus = () => ({ state: 'running', baseUrl: 'http://127.0.0.1:48200' });
  const urls = [];
  const fetchImpl = (url, options = {}) => {
    urls.push(url);
    if (!url.includes('?')) return Promise.resolve(response({ pending: [], version: 7 }));
    return new Promise((_resolve, reject) => {
      options.signal?.addEventListener('abort', () => {
        const err = new Error('aborted');
        err.name = 'AbortError';
        reject(err);
      });
    });
  };
  const ui = new ProxyDecisionController({
    engineManager, fetchImpl,
    dialog: { showMessageBox: async () => ({ response: 0 }) },
    pollMs: 5,
  });
  ui.start();
  await new Promise((resolve) => setTimeout(resolve, 30));
  try {
    assert.equal(urls.length, 2, '초기 조회 뒤에는 장기 대기 요청 하나만 있어야 한다');
    assert.match(urls[1], /\/decisions\?after=7&wait_ms=25000$/);
  } finally {
    ui.stop();
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
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

test('"마스킹본 바로 전송"을 켜면 창 없이 마스킹본을 보내고, 끄면 바로 다시 묻는다', async () => {
  let auto = true;
  let pulled = 0;
  const detail = {
    id: 'a1', fileName: '메시지', host: 'claude.ai',
    piiCount: 1, injectionCount: 0, forcedMaskCount: 0,
    scanStatus: 'ok', result: { scanStatus: 'ok' },
  };
  const { ui, posts, shown } = fixture(detail, [0], {
    autoSendMasked: () => auto, platform: 'darwin', getParentWindow: () => { pulled += 1; return null; },
  });
  await ui.checkNow();
  assert.equal(shown.length, 0, '켜 뒀는데 확인창을 띄웠다');
  assert.equal(pulled, 0, '묻지 않는데 대시보드를 앞으로 끌어냈다(맥)');
  assert.deepEqual(posts, [{ action: 'send_masked' }]);

  auto = false; // 설정에서 끔 — 다음 건부터
  const off = fixture({ ...detail, id: 'a2' }, [1], { autoSendMasked: () => auto });
  await off.ui.checkNow();
  assert.equal(off.shown.length, 1);
  assert.deepEqual(off.posts, [{ action: 'cancel' }]);
});

test('"마스킹본 바로 전송"을 켜도 검사를 끝내지 못한 건은 묻는다', async () => {
  const { ui, posts, shown } = fixture({
    id: 'a3', fileName: 'big.pdf', host: 'claude.ai',
    piiCount: 2, injectionCount: 0, forcedMaskCount: 0,
    scanStatus: 'ok', result: { scanStatus: 'ok', truncated: true },
  }, [0], { autoSendMasked: () => true }); // 검사 미완료 창의 0 = 취소
  await ui.checkNow();
  assert.equal(shown.length, 1, '일부만 검사한 마스킹본을 묻지 않고 보냈다');
  assert.deepEqual(posts, [{ action: 'cancel' }]);
});

// ── 맥 실측: 확인창이 앱을 막고, 끝난 건이 다시 뜨고, "취소" 가 엔진에 안 닿았다 ─────────────

test('결정을 엔진에 못 보내면 창을 다시 띄우지 않고 결정만 다시 보낸다', async () => {
  const detail = { id: 'r1', fileName: '메시지', host: 'claude.ai', piiCount: 1, scanStatus: 'ok', result: { scanStatus: 'ok' } };
  const posts = [];
  const shown = [];
  let failPost = true;
  const fetchImpl = async (url, options = {}) => {
    if (options.method === 'POST') {
      posts.push(JSON.parse(options.body));
      if (failPost) throw new Error('엔진 응답 없음');
      return response({ ok: true });
    }
    if (url.endsWith('/decisions')) return response({ pending: [{ id: 'r1' }] }); // 엔진은 아직 목록에 둔다
    return response(detail);
  };
  const ui = new ProxyDecisionController({
    engineManager: manager(), fetchImpl,
    dialog: { showMessageBox: async (opts) => { shown.push(opts); return { response: 1 }; } }, // 취소
  });
  await ui.checkNow();
  failPost = false;
  await ui.checkNow();
  await ui.checkNow();
  assert.equal(shown.length, 1, '이미 결정한 건의 창을 다시 띄웠다');
  assert.deepEqual(posts, [{ action: 'cancel' }, { action: 'cancel' }], '실패한 결정을 다시 보내야 한다(성공 뒤엔 그만)');
});

test('창이 떠 있는 동안 판단 건이 엔진에서 사라지면 창을 닫고 결정을 보내지 않는다', async () => {
  let detailCalls = 0;
  const posts = [];
  const fetchImpl = async (url, options = {}) => {
    if (options.method === 'POST') { posts.push(JSON.parse(options.body)); return response({ ok: true }); }
    if (url.endsWith('/decisions')) return response({ pending: [{ id: 'g1' }] });
    detailCalls += 1;
    return detailCalls === 1
      ? response({ id: 'g1', piiCount: 1, scanStatus: 'ok', result: { scanStatus: 'ok' } })
      : response({ detail: 'gone' }, 409);
  };
  let aborted = false;
  const notices = [];
  const ui = new ProxyDecisionController({
    engineManager: manager(), fetchImpl, watchMs: 5,
    dialog: {
      // 사람이 아무것도 누르지 않는다 — signal 로 닫혀야만 끝난다. signal 없는 창은 취소 안내다.
      showMessageBox: (opts) => (opts.signal ? new Promise((resolve) => {
        opts.signal.addEventListener('abort', () => { aborted = true; resolve({ response: opts.cancelId }); });
      }) : (notices.push(opts), Promise.resolve({ response: 0 }))),
    },
  });
  await ui.checkNow();
  assert.equal(aborted, true, '사라진 판단 건의 창이 닫히지 않았다');
  assert.deepEqual(posts, [], '사라진 건에 결정을 보냈다');
  // 사이트는 이유 없이 "전송 실패" 만 보인다 — 왜 안 갔는지 앱이 알려야 한다(Windows PX-6)
  assert.match(notices[0]?.message || '', /검토 시간이 지나 전송을 취소/);
  assert.equal(await ui.checkNow(), false, '사라진 건을 다시 띄웠다');
});

test('맥은 확인창을 대시보드 창에 붙인다(부모 없는 창은 앱 전체를 멈춘다)', async () => {
  const detail = { id: 'm1', piiCount: 1, scanStatus: 'ok', result: { scanStatus: 'ok' } };
  const fetchImpl = async (url, options = {}) => {
    if (options.method === 'POST') return response({ ok: true });
    if (url.endsWith('/decisions')) return response({ pending: [{ id: 'm1' }] });
    return response(detail);
  };
  const win = { id: 'dashboard' };
  const calls = [];
  const dialog = { showMessageBox: async (...args) => { calls.push(args); return { response: 0 }; } };
  await new ProxyDecisionController({
    engineManager: manager(), fetchImpl, dialog, platform: 'darwin', getParentWindow: () => win,
  }).checkNow();
  assert.equal(calls[0][0], win, '맥에서 부모 창 없이 띄웠다');
  assert.ok(calls[0][1].signal, '닫을 수 있게 signal 을 넘겨야 한다');

  calls.length = 0;
  await new ProxyDecisionController({
    engineManager: manager(), fetchImpl, dialog, platform: 'win32', getParentWindow: () => win,
  }).checkNow();
  assert.equal(typeof calls[0][0].message, 'string', 'Windows 는 지금처럼 부모 없이 띄운다');
});

test('프록시가 묻지 않고 막은 전송은 새 것만 운영체제 알림으로 띄운다(처음 받은 목록은 기준으로만)', async () => {
  let listed = { pending: [], version: 1, notices: [{ id: 3, host: 'chatgpt.com', reason: '앱 시작 전 일' }] };
  const notified = [];
  let dialogs = 0;
  const ui = new ProxyDecisionController({
    engineManager: manager(),
    dialog: { showMessageBox: async () => { dialogs += 1; return { response: 0 }; } },
    fetchImpl: async () => response(listed),
    notify: (title, body) => notified.push({ title, body }),
  });
  await ui.checkNow();
  assert.deepEqual(notified, [], '앱이 뜨기 전의 지난 알림을 띄웠다');
  listed = {
    pending: [], version: 2,
    notices: [
      { id: 3, host: 'chatgpt.com', reason: '앱 시작 전 일' },
      { id: 4, host: 'chatgpt.com', reason: '검사를 끝내지 못한 그림이라 보내지 않았습니다' },
    ],
  };
  await ui.checkNow();
  assert.equal(notified.length, 1);
  assert.match(notified[0].title, /chatgpt\.com/);
  assert.equal(notified[0].body, '검사를 끝내지 못한 그림이라 보내지 않았습니다');
  await ui.checkNow();
  assert.equal(notified.length, 1, '같은 알림을 또 띄웠다');
  assert.equal(dialogs, 0, '알림인데 확인창을 띄웠다');
});
