/**
 * inject-one-by-one.test.js
 *
 * injectOneByOne 이 "누가 파일을 받아갔는가" 를 잘못 읽던 문제를 지킨다.
 *
 * 왜 생겼나:
 *     합성 drop/paste 는 bubbles:true 로 쏜다. 그래서 dispatchEvent 의 반환값은
 *     **전파 경로 전체**의 preventDefault 를 반영한다 — 챗 UI 가 거의 예외 없이 걸어두는
 *     "브라우저가 드롭한 파일을 열지 않게" 하는 document 레벨 가드 하나만 있어도
 *     첫 후보에서 false 가 나왔다. 그러면 루프가 거기서 멈춰, 조상에 **직접** 쏴야
 *     먹는 사이트(Gemini xap-uploader-dropzone, 테스트 28)를 영영 못 쏜다.
 *     같은 커밋이 "조상 건너뛰기는 쓰지 않는다" 고 못박아 놓고, 반환값을 통해
 *     그 동작이 되살아나 있었다.
 *
 * 지키는 것:
 *     (1) 전역 가드만 있고 아무도 안 받아갔으면 후보를 끝까지 쏜다
 *     (2) 어떤 후보가 실제로 받아갔으면 그 자리에서 멈춘다(중복 첨부 방지 — 이게
 *         원래 injectOneByOne 이 존재하는 이유다)
 *     (3) 워처가 증거를 잡으면 즉시 멈춘다
 *
 * 실행: node extension/tests/inject-one-by-one.test.js  (exit 0 = 통과)
 */

const assert = require('assert');
const fs = require('fs');
const path = require('path');

// content.js 는 IIFE 라 export 가 없다. 검사 대상 함수만 원문에서 그대로 떼어 쓴다 —
// 복사본을 두면 원본이 바뀌어도 테스트는 계속 통과하는(=아무것도 안 지키는) 짝퉁이 된다.
const src = fs.readFileSync(path.join(__dirname, '..', 'content', 'content.js'), 'utf8');
const start = src.indexOf('  async function injectOneByOne(');
assert.ok(start > 0, 'injectOneByOne 을 content.js 에서 찾지 못했다 — 이름이 바뀌었나?');
const end = src.indexOf('\n  }\n', start);
assert.ok(end > start, 'injectOneByOne 의 끝을 찾지 못했다');
const injectOneByOne = new Function(
  'PER_TARGET_EVIDENCE_MS',
  `${src.slice(start, end + 4)}\nreturn injectOneByOne;`,
)(0);

/** 전파를 흉내내는 최소 타깃.
 *  순서: 사이트 리스너(우리보다 먼저 등록됨) → 우리 프로브 → 조상의 전역 가드(버블). */
function makeTarget(name, { siteTakes = false } = {}) {
  const listeners = [];
  return {
    name,
    addEventListener(_type, fn) { listeners.push(fn); },
    removeEventListener(_type, fn) {
      const i = listeners.indexOf(fn);
      if (i >= 0) listeners.splice(i, 1);
    },
    dispatch(globalGuard) {
      const e = { defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
      if (siteTakes) e.preventDefault();
      listeners.forEach((fn) => fn(e));
      if (globalGuard) e.preventDefault();
      return !e.defaultPrevented;   // dispatchEvent 의 반환값
    },
  };
}

function runner(globalGuard) {
  const fired = [];
  const fire = (t) => { fired.push(t.name); return t.dispatch(globalGuard); };
  return { fired, fire };
}

(async () => {
  // (1) 전역 가드가 모든 drop 을 취소하지만 아무도 받아가지 않았다 → 끝까지 쏜다.
  {
    const targets = [makeTarget('editor'), makeTarget('form'), makeTarget('dropzone')];
    const { fired, fire } = runner(true);
    const ok = await injectOneByOne('drop', targets, fire, null);
    assert.deepStrictEqual(
      fired, ['editor', 'form', 'dropzone'],
      `전역 가드 하나에 루프가 끊겼다 — 조상 후보를 못 쐈다 (쏜 곳: ${fired})`,
    );
    assert.strictEqual(ok, true, '한 번이라도 쐈으면 sent=true 여야 한다');
  }

  // (2) 두 번째 후보가 실제로 받아갔다(전역 가드도 함께 있다) → 세 번째는 쏘지 않는다.
  {
    const targets = [makeTarget('editor'), makeTarget('dropzone', { siteTakes: true }), makeTarget('body')];
    const { fired, fire } = runner(true);
    const ok = await injectOneByOne('drop', targets, fire, null);
    assert.deepStrictEqual(
      fired, ['editor', 'dropzone'],
      `받아간 대상 뒤로 더 쐈다 — 같은 파일이 두 번 첨부된다 (쏜 곳: ${fired})`,
    );
    assert.strictEqual(ok, true);
  }

  // (3) 워처가 증거를 잡으면 그 자리에서 멈춘다(기존 동작).
  {
    const targets = [makeTarget('editor'), makeTarget('form')];
    const { fired, fire } = runner(false);
    const watcher = { settle: async () => ({ ok: true }) };
    await injectOneByOne('drop', targets, fire, watcher);
    assert.deepStrictEqual(fired, ['editor'], `워처가 ok 인데 계속 쐈다 (쏜 곳: ${fired})`);
  }

  console.log('inject-one-by-one.test.js: 3개 통과');
})().catch((e) => { console.error(e); process.exit(1); });
