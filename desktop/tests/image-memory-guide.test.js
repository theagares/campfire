'use strict';
/**
 * 이미지 검사 메모리 안내 — 엔진이 메모리 때문에 문서 속 그림을 뺐다고 알리면(/health imageOcr.memoryShort):
 * 커밋 한도가 작으면 설정 화면에 페이지 파일 늘리는 법을 접어 두고 대시보드 VARCO 캡션을 누르면 펼친다,
 * 물리 메모리면 다른 프로그램을 닫으라고만 한다, 부족이 없거나 라이트 판이면 아무것도 띄우지 않는다.
 * (맥·라이트·이미지 검사 끔은 main/ipc.js 가 걸러 memoryShort 를 null 로 넘긴다 — model-status.test.js)
 */

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const rendererDir = path.join(__dirname, '..', 'renderer');
const APP = fs.readFileSync(path.join(rendererDir, 'app.js'), 'utf8');
const HTML = fs.readFileSync(path.join(rendererDir, 'index.html'), 'utf8');

/** app.js 를 스텁 DOM 위에 올린다 — 선택자마다 같은 가짜 요소를 돌려준다. */
function loadRenderer() {
  const els = new Map();
  const makeEl = () => {
    const classes = new Set();
    const attrs = new Map();
    const handlers = {};
    return {
      textContent: '', innerHTML: '', value: '', checked: false, open: false, hidden: false, title: '', tabIndex: -1,
      style: {}, dataset: {},
      classList: {
        add: (c) => classes.add(c), remove: (c) => classes.delete(c), contains: (c) => classes.has(c),
        toggle: (c, on = !classes.has(c)) => { if (on) classes.add(c); else classes.delete(c); return on; },
      },
      setAttribute: (k, v) => attrs.set(k, String(v)), getAttribute: (k) => (attrs.has(k) ? attrs.get(k) : null),
      removeAttribute: (k) => attrs.delete(k),
      addEventListener: (ev, fn) => { (handlers[ev] = handlers[ev] || []).push(fn); },
      fire: (ev, extra = {}) => (handlers[ev] || []).forEach((fn) => fn({ preventDefault() {}, target: null, ...extra })),
      closest: () => makeEl(), querySelector: () => makeEl(), querySelectorAll: () => [],
      appendChild() {}, replaceChildren() {}, scrollIntoView() { this.scrolled = true; }, focus() {},
      getBoundingClientRect: () => ({ top: 0, left: 0, width: 0, height: 0 }),
    };
  };
  const el = (sel) => {
    if (!els.has(sel)) els.set(sel, makeEl());
    return els.get(sel);
  };
  // 메인 프로세스 응답은 오지 않는 것으로 둔다 — 이 시험은 그리기만 본다(init·설정 목록 읽기가 시험 뒤에 돌지 않게)
  const api = new Proxy({}, { get: (_t, key) => (String(key).startsWith('on') ? () => {} : () => new Promise(() => {})) });
  const sandbox = {
    console: { log() {}, warn() {}, error() {} },
    setTimeout, clearTimeout, setInterval: () => 0, clearInterval() {},
    requestAnimationFrame: () => 0, cancelAnimationFrame() {},
    navigator: { clipboard: { writeText: () => Promise.resolve() } },
    document: {
      querySelector: el, querySelectorAll: () => [], getElementById: (id) => el(`#${id}`),
      addEventListener() {}, createElement: () => makeEl(), visibilityState: 'visible', title: '',
    },
    upsec: api,
  };
  sandbox.window = sandbox;
  const ctx = vm.createContext(sandbox);
  vm.runInContext(APP, ctx, { filename: 'app.js' });
  return { ctx, el, run: (code) => vm.runInContext(code, ctx) };
}

const COMMIT = { kind: 'commit', freeMb: 5423, needMb: 9216, commitLimitMb: 33669, at: '2026-10-30T09:47:00+0900' };
const imageModel = (memoryShort) => ({
  name: 'VARCO-VISION 2.0 1.7B', enabled: true, ready: true, warning: false, label: '작동 중 · CUDA', memoryShort,
});
const stats = (memoryShort) => ({ models: { imageOcr: imageModel(memoryShort) }, trend: [] });

test('안내 상자는 기존 .banner.warn 으로 기본 숨김이고, 방법은 눌러야 펼쳐진다', () => {
  assert.match(HTML, /<div class="banner warn" id="image-ocr-memory" style="display:none">/);
  assert.match(HTML, /<details id="image-ocr-memory-guide">\s*<summary>해결 방법 보기<\/summary>/);
  for (const step of ['고급 시스템 설정 보기', '성능의 [설정]', '가상 메모리의 [변경]', '사용자 지정 크기', '16384', '32768', '다시 시작']) {
    assert.ok(HTML.includes(step), `페이지 파일 안내에 "${step}" 이 없다`);
  }
  assert.match(HTML, /Campfire 는 이 설정을 바꾸지 않습니다/);
});

test('커밋 한도가 작으면 설정에 방법과 지금 한도·모자란 양을, 캡션은 눌러서 여는 안내로 바꾼다', () => {
  const r = loadRenderer();
  r.ctx.renderImageMemory(imageModel(COMMIT));
  assert.equal(r.el('#image-ocr-memory').style.display, '');
  assert.equal(r.el('#image-ocr-memory-guide').style.display, '');
  assert.match(r.el('#image-ocr-memory-text').textContent, /^PC 메모리 한도\(커밋\)가 작아 문서 속 그림을 검사하지 못하고 빼고 보냈습니다\. Windows 페이지 파일을 늘리면 그림도 검사합니다\.$/);
  assert.match(r.el('#image-ocr-memory-size').textContent, /지금 커밋 한도 32\.9GB/);
  assert.match(r.el('#image-ocr-memory-size').textContent, /남은 5\.3GB 로 약 3\.7GB 모자랐습니다/);
  const caption = r.el('#dash-image-label');
  assert.equal(caption.textContent, '메모리 한도 부족으로 그림 제외 · 해결 방법');
  assert.ok(caption.classList.contains('has-guide') && caption.getAttribute('role') === 'button' && caption.tabIndex === 0);

  caption.fire('click');
  assert.ok(r.el('#settings-modal').classList.contains('open'), '캡션을 누르면 설정이 열려야 한다');
  assert.equal(r.el('#image-ocr-memory-guide').open, true, '해결 방법이 펼쳐져 있어야 한다');
  assert.ok(r.el('#image-ocr-memory').scrolled, '안내가 보이게 스크롤해야 한다');
});

test('물리 메모리면 다른 프로그램을 닫으라고만 하고, 방법 상자와 누르기는 없다', () => {
  const r = loadRenderer();
  r.ctx.renderImageMemory(imageModel({ ...COMMIT, kind: 'phys', freeMb: 900, needMb: 1536 }));
  assert.equal(r.el('#image-ocr-memory').style.display, '');
  assert.equal(r.el('#image-ocr-memory-guide').style.display, 'none');
  assert.equal(r.el('#image-ocr-memory-text').textContent, 'PC 메모리가 부족해 문서 속 그림을 빼고 보냈습니다. 다른 프로그램을 닫고 다시 보내 주세요.');
  const caption = r.el('#dash-image-label');
  assert.equal(caption.textContent, '메모리 부족으로 그림 제외 · 다른 프로그램 닫기');
  assert.ok(!caption.classList.contains('has-guide') && caption.getAttribute('role') === null);
  caption.fire('click');
  assert.ok(!r.el('#settings-modal').classList.contains('open'));
});

test('부족이 없으면 안내를 숨기고 캡션은 원래 모델 상태로 돌아간다', () => {
  const r = loadRenderer();
  r.run(`state.stats = ${JSON.stringify(stats(COMMIT))}; renderDashboard();`);
  assert.equal(r.el('#image-ocr-memory').style.display, '');
  r.run(`state.stats = ${JSON.stringify(stats(null))}; renderDashboard();`);
  assert.equal(r.el('#image-ocr-memory').style.display, 'none');
  assert.equal(r.el('#dash-image-label').textContent, 'VARCO-VISION 2.0 1.7B · 작동 중 · CUDA');
  assert.ok(!r.el('#dash-image-label').classList.contains('has-guide'));
});

test('라이트 판은 VARCO 카드와 함께 안내도 그리지 않는다', () => {
  const r = loadRenderer();
  r.run(`state.lite = true; state.stats = ${JSON.stringify(stats(COMMIT))}; renderDashboard();`);
  assert.notEqual(r.el('#image-ocr-memory').style.display, '');
  assert.equal(r.el('#dash-image-label').textContent, '');
});
