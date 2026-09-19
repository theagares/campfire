/**
 * multi-attach.test.js
 *
 * 다중 첨부의 계약을 SW 쪽에서 지킨다. content-regression 은 페이지 쪽(수집·주입)을
 * 보고, 여기서는 **배치 하나가 순차로 검사되고 결정이 산출물로 바뀌는** 부분을 본다.
 *
 * 왜 필요한가: 원래 버그는 "파일을 여러 개 고르면 마지막 것만 된다" 였다. 그 반대편,
 * 즉 N개가 N개로 끝까지 살아남는지는 단언이 없으면 언제든 다시 하나로 줄어든다.
 *
 * 실행: node tests/multi-attach.test.js   (exit 0 = 통과)
 */

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const read = (...seg) => fs.readFileSync(path.join(__dirname, '..', ...seg), 'utf8');

// SW 는 module 이다 — import 를 걷어내고 그 심볼은 아래에서 스텁으로 넣는다
// (sw-session-persistence.test.js 와 같은 수법).
const SW = read('background', 'service-worker.js')
  .replace(/^import\s+\{[\s\S]*?\}\s+from\s+'[^']+';\s*$/gm, '')
  .replace(/^import\s+[^\n]*from\s+'[^']+';\s*$/gm, '');
const SHARED = read('utils', 'mask-segments.js').replace(/^export /gm, '');

// ── 스텁 ────────────────────────────────────────────────────────────────────
const sessionStore = {};
const panelMessages = [];
const tabMessages = [];
let swListener = null;

// 엔진 응답. 파일마다 다른 결과를 주기 위해 fileName 으로 갈라 놓는다.
const engineResults = {
  'a.pdf': { scanStatus: 'ok', originalText: '김철수 보고서', piiItems: [{ start: 0, end: 3, type: 'PERSON_NAME' }], injectionItems: [], truncated: false, scannedChars: 7, originalChars: 7 },
  'b.pdf': { scanStatus: 'ok', originalText: '박영희 계약서', piiItems: [{ start: 0, end: 3, type: 'PERSON_NAME' }], injectionItems: [], truncated: false, scannedChars: 7, originalChars: 7 },
  // 잘린 파일: 뒷부분을 안 봤다는 사실이 상태로 드러나야 한다
  'long.pdf': { scanStatus: 'ok', originalText: '앞부분만', piiItems: [], injectionItems: [], truncated: true, scannedChars: 4, originalChars: 900 },
};

const sandbox = {
  console: { log() {}, warn() {}, error() {}, debug() {} },
  setTimeout, clearTimeout, setInterval, clearInterval, queueMicrotask,
  btoa: (b) => Buffer.from(b, 'binary').toString('base64'),
  atob: (b) => Buffer.from(b, 'base64').toString('binary'),
  fetch: () => Promise.resolve({ ok: true, json: () => Promise.resolve({}) }),
  TextEncoder, Blob, FormData, URL, crypto: { randomUUID: () => 'uuid-' + Math.random() },
  chrome: {
    runtime: {
      onMessage: { addListener: (l) => { swListener = l; }, removeListener() {} },
      onInstalled: { addListener() {} },
      onStartup: { addListener() {} },
      lastError: null,
      sendMessage: (m) => { panelMessages.push(m); return { catch() {} }; },
      getURL: (p) => p,
    },
    storage: {
      session: {
        get: (k) => Promise.resolve({ [k]: sessionStore[k] }),
        set: (o) => { Object.assign(sessionStore, o); return Promise.resolve(); },
        remove: () => Promise.resolve(),
      },
      local: { get: () => Promise.resolve({}), set: () => Promise.resolve() },
      onChanged: { addListener() {} },
    },
    tabs: {
      sendMessage: (tabId, m) => { tabMessages.push({ tabId, ...m }); return Promise.resolve(); },
      onRemoved: { addListener() {} },
      onUpdated: { addListener() {} },
      query: () => Promise.resolve([]),
availabilityPlaceholder: undefined,
    },
    action: { setBadgeText() {}, setBadgeBackgroundColor() {}, setTitle() {} },
    sidePanel: { setOptions: () => ({ catch() {} }), open: () => ({ catch() {} }), setPanelBehavior: () => ({ catch() {} }) },
    alarms: undefined,
    declarativeNetRequest: undefined,
  },
};
sandbox.self = sandbox;
sandbox.globalThis = sandbox;
sandbox.window = sandbox;

const ctx = vm.createContext(sandbox);
vm.runInContext(SHARED, ctx, { filename: 'mask-segments.js' });
// docwrapper 의 import 를 대신한다 — MD 고정이라는 계약만 지키면 된다.
vm.runInContext(
  "function wrapMaskedFile(text, mime, name) {"
  + " return { bytes: new TextEncoder().encode(text), mimeType: 'text/plain',"
  + " fileName: String(name || 'doc').replace(/(\\.[^.]+)?$/, '_masked.md') }; }",
  ctx, { filename: 'docwrapper-stub.js' },
);
vm.runInContext(SW, ctx, { filename: 'service-worker.js' });
assert.ok(swListener, 'SW 가 onMessage 리스너를 달지 않았다 — 하네스가 스크립트를 건너뛴 것');

// 엔진 호출을 가로채 파일별 결과를 돌려준다.
sandbox.scanPrompt = undefined;
vm.runInContext(
  "scanPrompt = async () => ({ scanStatus: 'ok', originalText: '요약해줘', piiItems: [], injectionItems: [] });",
  ctx, { filename: 'stub.js' },
);
vm.runInContext(
  "scanCombined = async (p) => { lastWrapFile = p.wrapFile; return __engine[p.fileName]; };",
  ctx, { filename: 'stub.js' },
);
sandbox.__engine = engineResults;

// ── 헬퍼 ────────────────────────────────────────────────────────────────────
const send = (msg, sender = { tab: { id: 7 } }) => new Promise((resolve) => {
  const async_ = swListener(msg, sender, resolve);
  if (!async_) resolve(undefined);
});
const settle = () => new Promise(r => setTimeout(r, 30));
const panelOf = (type) => panelMessages.filter(m => m.type === type);

(async () => {
  // ── 1) 배치 시작 → 탭 메타가 즉시, 순서대로 나온다 ───────────────────────
  await send({
    type: 'START_MULTI_SCAN',
    sessionId: 's1',
    payload: {
      items: [
        { id: 'f0', fileName: 'a.pdf', fileSize: 10, mimeType: 'application/pdf', supported: true },
        { id: 'f1', fileName: 'b.pdf', fileSize: 20, mimeType: 'application/pdf', supported: true },
      ],
    },
  });
  await settle();

  const init = panelOf('PANEL_SCAN_INIT')[0];
  assert.ok(init, 'PANEL_SCAN_INIT 이 없다 — 패널이 탭을 그릴 수 없다');
  assert.strictEqual(init.docs.length, 2, '파일 2개가 그대로 와야 한다');
  assert.strictEqual(init.docs.map(d => d.fileName).join(','), 'a.pdf,b.pdf', '스테이징 순서 유지');

  // lease 는 인코딩보다 먼저 온다 — content 는 이걸 받기 전에 base64 를 만들지 않는다.
  const grant = tabMessages.find(m => m.type === 'SCAN_LEASE_GRANTED');
  assert.ok(grant, 'lease 를 주지 않았다 — content 가 영원히 기다린다');
  const leaseId = grant.leaseId;

  // ── 2) lease 가 없는 메시지는 상태를 바꾸지 못한다 ───────────────────────
  const bad = await send({ type: 'SCAN_MULTI_ITEM', sessionId: 's1', leaseId: 'wrong', payload: { docId: 'f0' } });
  assert.strictEqual(bad?.ok, false, '잘못된 leaseId 를 받아들였다');

  const otherTab = await send(
    { type: 'SCAN_MULTI_ITEM', sessionId: 's1', leaseId, payload: { docId: 'f0' } },
    { tab: { id: 99 } },
  );
  assert.strictEqual(otherTab?.ok, false, '다른 탭의 메시지를 받아들였다');

  // ── 3) 프롬프트 먼저, 그 다음 파일 ───────────────────────────────────────
  await send({ type: 'SCAN_MULTI_PROMPT', sessionId: 's1', leaseId, text: '요약해줘' });
  await settle();
  assert.strictEqual(panelOf('PANEL_SCAN_PROMPT').length, 1, '프롬프트 결과가 1건이어야 한다');

  for (const [docId, fileName] of [['f0', 'a.pdf'], ['f1', 'b.pdf']]) {
    await send({
      type: 'SCAN_MULTI_ITEM', sessionId: 's1', leaseId,
      payload: { docId, fileName, mimeType: 'application/pdf', base64Data: 'x', userPrompt: '요약해줘' },
    });
  }
  await settle();

  // 다중은 검사 시점에 바이너리를 만들지 않는다.
  assert.strictEqual(sandbox.lastWrapFile, false, '다중 검사에 wrapFile=false 가 안 갔다');

  const items = panelOf('PANEL_SCAN_ITEM');
  assert.strictEqual(items.length, 2, '파일 2개 결과가 각각 와야 한다');
  assert.strictEqual(items.map(m => m.doc.id).join(','), 'f0,f1', '파일별로 따로 도착');
  // 브로드캐스트에 원문이 실리면 안 된다 — runtime 채널은 전역이다.
  for (const m of items) {
    assert.ok(!('originalText' in m.doc), 'PANEL_SCAN_ITEM 에 원문이 실렸다');
  }

  // ── 4) 본문은 요청한 탭에만, 요청한 항목만 ───────────────────────────────
  const mine = await send({ type: 'GET_PANEL_ITEM_RESULT', sessionId: 's1', itemId: 'f0' });
  assert.strictEqual(mine.ok, true);
  assert.strictEqual(mine.originalText, '김철수 보고서');

  const theirs = await send(
    { type: 'GET_PANEL_ITEM_RESULT', sessionId: 's1', itemId: 'f0' }, { tab: { id: 99 } },
  );
  assert.strictEqual(theirs.ok, false, '다른 탭이 남의 문서 본문을 읽었다');

  await send({ type: 'FINISH_MULTI_SCAN', sessionId: 's1', leaseId });
  await settle();
  assert.strictEqual(panelOf('PANEL_SCAN_DONE').length, 1);

  // ── 5) 결정 → 파일마다 자기 산출물 ───────────────────────────────────────
  const decided = await send({
    type: 'PANEL_MULTI_DECISION',
    sessionId: 's1',
    decision: {
      action: 'send',
      prompt: { action: 'masked', unmaskedKeys: [] },
      files: [
        { id: 'f0', action: 'masked', unmaskedKeys: [] },        // 전부 마스킹
        { id: 'f1', action: 'masked', unmaskedKeys: ['f1:0'] },  // 사용자가 해제
      ],
    },
  });
  assert.strictEqual(decided.ok, true, `결정이 거부됐다: ${JSON.stringify(decided)}`);

  const relay = tabMessages.filter(m => m.type === 'CONTENT_BATCH_DECISION').slice(-1)[0];
  assert.ok(relay, 'content 로 결정이 중계되지 않았다');
  assert.strictEqual(relay.decision.files.length, 2, 'N개가 N개로 남아야 한다');
  // 결정 메시지에는 바이너리가 없다 — artifactId 만 간다.
  const asText = JSON.stringify(relay.decision);
  assert.ok(!asText.includes('base64'), '결정 메시지에 바이너리가 실렸다');
  const ids = relay.decision.files.map(f => f.artifactId);
  assert.strictEqual(new Set(ids).size, 2, '두 파일이 같은 산출물을 가리킨다');

  // ── 6) 산출물은 한 개씩, 자기 것만 ───────────────────────────────────────
  const a0 = await send({ type: 'GET_SCAN_ARTIFACT', sessionId: 's1', docId: 'f0', artifactId: ids[0] });
  assert.strictEqual(a0.ok, true);
  const text0 = Buffer.from(a0.base64, 'base64').toString('utf8');
  assert.strictEqual(text0, '[이름 마스킹] 보고서', '아무것도 해제 안 한 파일은 전부 마스킹');

  const a1 = await send({ type: 'GET_SCAN_ARTIFACT', sessionId: 's1', docId: 'f1', artifactId: ids[1] });
  const text1 = Buffer.from(a1.base64, 'base64').toString('utf8');
  assert.strictEqual(text1, '박영희 계약서', '사용자가 해제한 항목은 원문 유지');

  // 문서 짝이 안 맞으면 거절한다 — artifactId 는 저장소 키가 아니라 능력 토큰이다.
  const cross = await send({ type: 'GET_SCAN_ARTIFACT', sessionId: 's1', docId: 'f0', artifactId: ids[1] });
  assert.strictEqual(cross.ok, false, '다른 문서의 산출물을 내줬다');

  const foreign = await send(
    { type: 'GET_SCAN_ARTIFACT', sessionId: 's1', docId: 'f0', artifactId: ids[0] }, { tab: { id: 99 } },
  );
  assert.strictEqual(foreign.ok, false, '다른 탭에 산출물을 내줬다');

  // ACK 전까지는 다시 받을 수 있어야 한다(응답 유실 대비).
  const again = await send({ type: 'GET_SCAN_ARTIFACT', sessionId: 's1', docId: 'f0', artifactId: ids[0] });
  assert.strictEqual(again.ok, true, 'ACK 전인데 재요청이 거절됐다');
  await send({ type: 'ACK_SCAN_ARTIFACT', artifactId: ids[0] });
  const consumed = await send({ type: 'GET_SCAN_ARTIFACT', sessionId: 's1', docId: 'f0', artifactId: ids[0] });
  assert.strictEqual(consumed.ok, false, 'ACK 뒤에도 산출물이 남아 있다');

  // ── 7) finalize 가 세션과 남은 산출물을 지운다 ───────────────────────────
  await send({ type: 'FINALIZE_MULTI_SESSION', sessionId: 's1', status: 'ok' });
  await settle();
  const leftover = await send({ type: 'GET_SCAN_ARTIFACT', sessionId: 's1', docId: 'f1', artifactId: ids[1] });
  assert.strictEqual(leftover.ok, false, 'finalize 뒤에도 산출물이 남아 있다');

  // 세션 스냅샷에 원문/바이너리가 남지 않아야 한다(디스크에 안 쓰는 것과 별개로,
  // 마스킹본은 storage 로 아예 나가면 안 된다 — persistSessions 가 통째로 직렬화한다).
  const dump = JSON.stringify(sessionStore);
  assert.ok(!dump.includes('maskedBase64'), 'storage 에 마스킹 바이너리가 새어 나갔다');

  // ── 8) 잘린 파일은 done 이 아니라 truncated ──────────────────────────────
  panelMessages.length = 0;
  tabMessages.length = 0;
  await send({
    type: 'START_MULTI_SCAN', sessionId: 's2',
    payload: { items: [{ id: 'f0', fileName: 'long.pdf', fileSize: 10, mimeType: 'application/pdf', supported: true }] },
  });
  await settle();
  const lease2 = tabMessages.find(m => m.type === 'SCAN_LEASE_GRANTED').leaseId;
  await send({ type: 'SCAN_MULTI_PROMPT', sessionId: 's2', leaseId: lease2, text: '요약해줘' });
  await send({
    type: 'SCAN_MULTI_ITEM', sessionId: 's2', leaseId: lease2,
    payload: { docId: 'f0', fileName: 'long.pdf', mimeType: 'application/pdf', base64Data: 'x', userPrompt: '요약해줘' },
  });
  await settle();

  const tdoc = panelOf('PANEL_SCAN_ITEM').slice(-1)[0].doc;
  assert.strictEqual(tdoc.status, 'truncated', '잘린 파일이 done 으로 표시됐다 — 사용자가 안전하다고 읽는다');
  assert.strictEqual(tdoc.scannedChars, 4);
  assert.strictEqual(tdoc.originalChars, 900);

  // s2 를 끝내 lease 를 푼다. 안 풀면 다음 배치는 큐에서 계속 기다린다 —
  // 그게 전역 큐가 할 일이지만, 여기서는 다음 블록을 돌려야 하므로 정상 종료시킨다.
  await send({ type: 'FINISH_MULTI_SCAN', sessionId: 's2', leaseId: lease2 });
  await settle();

  // ── 9) 읽지 못한 파일은 대기가 아니라 오류다 ─────────────────────────────
  //     그냥 건너뛰면 그 탭이 영원히 '대기 중' 으로 남아 전송 버튼이 열리지 않고,
  //     사용자에게는 "기다리면 되는" 것처럼 보인다. 오류로 세워야 "이 파일 제거" 를
  //     골라 나머지를 보낼 수 있다.
  panelMessages.length = 0;
  tabMessages.length = 0;
  await send({
    type: 'START_MULTI_SCAN', sessionId: 's3',
    payload: { items: [{ id: 'f0', fileName: 'unreadable.pdf', fileSize: 10, mimeType: 'application/pdf', supported: true }] },
  });
  await settle();
  const lease3 = tabMessages.find(m => m.type === 'SCAN_LEASE_GRANTED').leaseId;
  await send({ type: 'SCAN_MULTI_PROMPT', sessionId: 's3', leaseId: lease3, text: '요약해줘' });
  await send({
    type: 'SCAN_MULTI_ITEM', sessionId: 's3', leaseId: lease3,
    payload: { docId: 'f0', readError: '파일을 읽지 못했습니다' },
  });
  await settle();

  const rdoc = panelOf('PANEL_SCAN_ITEM').slice(-1)[0].doc;
  assert.strictEqual(rdoc.status, 'error', '읽기 실패가 대기 상태로 남았다 — 전송이 영영 안 열린다');
  assert.ok(rdoc.error, '오류 사유가 없어 사용자가 왜 막혔는지 모른다');

  // ── 10) 결정 전 선택은 SW 가 들고 있다가 돌려준다 ────────────────────────
  //     이게 없으면 검토 도중 패널을 닫았다 열었을 때 검사 결과와 탭은 돌아오는데
  //     **해제한 항목과 파일별 선택만 초기화된다.** 긴 문서에서 수십 개를 하나씩
  //     풀어 둔 사람에게는 처음부터 다시 하라는 뜻이다.
  panelMessages.length = 0;
  tabMessages.length = 0;
  await send({
    type: 'START_MULTI_SCAN', sessionId: 's4',
    payload: { items: [{ id: 'f0', fileName: 'draft.pdf', fileSize: 10, mimeType: 'application/pdf', supported: true }] },
  });
  await settle();

  await send({ type: 'PANEL_DRAFT_UPDATE', sessionId: 's4', unmaskedKeys: ['f0:0', 'f0:2'] });
  await send({ type: 'PANEL_DRAFT_UPDATE', sessionId: 's4', activeTab: 'prompt' });
  await send({ type: 'PANEL_DRAFT_UPDATE', sessionId: 's4', decisions: { f0: 'original' } });

  const snap = await send({ type: 'PANEL_READY', tabId: 7 }, { tab: { id: 7 } });
  const draft = snap?.session?.draft;
  assert.ok(draft, 'draft 가 세션에 안 붙었다');
  assert.strictEqual((draft.unmaskedKeys || []).join(','), 'f0:0,f0:2', '해제 목록이 안 돌아왔다');
  assert.strictEqual(draft.activeTab, 'prompt', '활성 탭이 안 돌아왔다');
  assert.strictEqual(draft.decisions?.f0, 'original', '파일별 선택이 안 돌아왔다');

  // 부분 갱신이어야 한다 — 탭만 알린 메시지가 해제 목록을 지우면 안 된다.
  await send({ type: 'PANEL_DRAFT_UPDATE', sessionId: 's4', activeTab: 'f0' });
  const snap2 = await send({ type: 'PANEL_READY', tabId: 7 }, { tab: { id: 7 } });
  assert.strictEqual(
    (snap2.session.draft.unmaskedKeys || []).join(','), 'f0:0,f0:2',
    '탭 전환이 해제 목록을 날렸다',
  );

  // 다른 탭은 남의 draft 를 못 쓴다.
  const foreignDraft = await send(
    { type: 'PANEL_DRAFT_UPDATE', sessionId: 's4', unmaskedKeys: [] }, { tab: { id: 99 } },
  );
  assert.strictEqual(foreignDraft.ok, false, '다른 탭이 남의 draft 를 덮어썼다');

  console.log('multi-attach.test.js: 10개 블록 통과');
})().catch((e) => { console.error(e); process.exit(1); });
