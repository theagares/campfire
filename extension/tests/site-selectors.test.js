/**
 * site-selectors.test.js
 *
 * PROMPT_CONFIGS 의 사이트별 선택자가 **실측된 DOM 앵커**를 계속 담고 있는지 지킨다.
 *
 * 왜 생겼나 (2026-09-06 실브라우저 검증):
 *   - perplexity.ai: 컴포저가 #ask-input(Lexical)로 바뀌고 aria-label 이 사라져,
 *     기존 editorSel('textarea[placeholder], [contenteditable="true"][aria-label]')이
 *     **아무것도 잡지 못했다**. 구버전 번들에서는 그 결과 getEditorText() 가 빈
 *     문자열을 주고 가로채기가 조용히 빠져나가, 질의가 검사 없이 전송됐다.
 *   - copilot.microsoft.com: 전송 버튼 aria-label 이 "메시지 제출" 로 바뀌어
 *     "제출" 정확일치가 빗나갔다. 지금은 GENERIC_SEND_SELS 의 부분일치가 구제하고
 *     있지만, 그건 "사이트 선택자가 하나도 안 맞을 때" 만 붙는 안전망이다.
 *     안전망에 기대는 상태를 정상으로 두면, 다음에 진짜로 깨졌을 때 구분이 안 된다.
 *
 * 이 테스트의 한계(중요): 이 환경에는 jsdom 이 없어 **실제 DOM 매칭을 검사하지
 * 못한다**. 대신 실측으로 확인한 안정적 앵커(id / data-testid 등)가 설정에 남아
 * 있는지와, 죽은 것으로 확인된 선택자가 되살아나지 않았는지를 고정한다.
 * 사이트가 또 바뀌면 이 테스트는 통과하면서도 실제로는 깨질 수 있다 — 그때는
 * 실브라우저 계측이 다시 필요하다. 그 사실까지 여기 적어둔다.
 *
 * 실행: node tests/site-selectors.test.js   (exit 0 = 통과)
 */

const fs = require('fs');
const path = require('path');
const assert = require('assert');

const SRC = fs.readFileSync(path.join(__dirname, '..', 'content', 'content.js'), 'utf8');

/** content.js 에서 PROMPT_CONFIGS 블록만 떼어내 사이트별 선택자 문자열을 읽는다. */
function readConfigs() {
  const start = SRC.indexOf('const PROMPT_CONFIGS = {');
  assert.ok(start !== -1, 'PROMPT_CONFIGS 를 찾지 못했다 — 이름이 바뀌었나?');
  const body = SRC.slice(start, SRC.indexOf('\n  };', start));

  const out = {};
  const siteRe = /'([a-z0-9.]+)':\s*\{([\s\S]*?)\n    \}/g;
  let m;
  while ((m = siteRe.exec(body))) {
    const [, host, block] = m;
    const pick = (key) => {
      const r = new RegExp(`${key}:\\s*'([^']*)'`).exec(block);
      return r ? r[1] : null;
    };
    out[host] = { editorSel: pick('editorSel'), sendBtnSel: pick('sendBtnSel') };
  }
  return out;
}

const CONFIGS = readConfigs();

// 6개 사이트가 전부 있어야 한다 — 하나라도 빠지면 그 사이트는 무방비다.
const SITES = [
  'chatgpt.com', 'claude.ai', 'gemini.google.com',
  'grok.com', 'perplexity.ai', 'copilot.microsoft.com',
];

for (const host of SITES) {
  const cfg = CONFIGS[host];
  assert.ok(cfg, `${host} 설정이 없다`);
  assert.ok(cfg.editorSel, `${host}: editorSel 이 비었다`);
  assert.ok(cfg.sendBtnSel, `${host}: sendBtnSel 이 비었다`);
}

// ── perplexity.ai — 2026-09-06 실측 ──────────────────────────────────────────
{
  const { editorSel, sendBtnSel } = CONFIGS['perplexity.ai'];

  assert.ok(
    editorSel.includes('#ask-input'),
    'perplexity: 실측된 컴포저 id(#ask-input)가 editorSel 에서 빠졌다',
  );
  // 죽은 선택자가 되살아나면(= 이 두 개만 남으면) 다시 아무것도 못 잡는다.
  assert.ok(
    !/^textarea\[placeholder\], \[contenteditable="true"\]\[aria-label\]$/.test(editorSel),
    'perplexity: 아무것도 잡지 못하던 예전 editorSel 로 되돌아갔다',
  );
  assert.ok(
    sendBtnSel.includes('"제출"') || sendBtnSel.includes('*="제출"'),
    'perplexity: 한국어 UI 의 전송 버튼(aria-label="제출")이 sendBtnSel 에 없다',
  );
}

// ── copilot.microsoft.com — 2026-09-06 실측 ──────────────────────────────────
{
  const { editorSel, sendBtnSel } = CONFIGS['copilot.microsoft.com'];

  assert.ok(
    editorSel.includes('composer-input'),
    'copilot: 실측된 data-testid(composer-input)가 editorSel 에서 빠졌다',
  );
  assert.ok(
    sendBtnSel.includes('submit-button'),
    'copilot: 실측된 data-testid(submit-button)가 sendBtnSel 에서 빠졌다',
  );
  // "메시지 제출" 을 놓치던 정확일치로 되돌아가면 다시 폴백 의존 상태가 된다.
  assert.ok(
    !/aria-label="제출"/.test(sendBtnSel) || /aria-label\*="제출"/.test(sendBtnSel),
    'copilot: aria-label="제출" 정확일치는 실제 라벨("메시지 제출")을 놓친다',
  );
}

// ── 언어 의존 선택자를 쓰는 사이트는 영어/한국어 양쪽을 담아야 한다 ──────────
// aria-label 은 UI 언어를 탄다. 한쪽만 담으면 다른 언어 사용자에게서 조용히 깨진다.
for (const host of SITES) {
  const { sendBtnSel } = CONFIGS[host];
  const usesAriaLabel = sendBtnSel.includes('aria-label');
  if (!usesAriaLabel) continue;                   // data-testid 등 언어 무관이면 통과
  const hasKo = /제출|보내기/.test(sendBtnSel);
  const hasEn = /submit|send/i.test(sendBtnSel);
  assert.ok(
    hasKo && hasEn,
    `${host}: aria-label 기반 sendBtnSel 인데 한국어/영어 중 한쪽만 있다 (${sendBtnSel})`,
  );
}

console.log('site-selectors ok');
