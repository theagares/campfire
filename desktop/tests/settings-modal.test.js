'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const rendererDir = path.join(__dirname, '..', 'renderer');
const app = fs.readFileSync(path.join(rendererDir, 'app.js'), 'utf8');
const css = fs.readFileSync(path.join(rendererDir, 'styles.css'), 'utf8');

function rule(selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = css.match(new RegExp(`${escaped}\\s*\\{([^}]+)\\}`));
  assert.ok(match, `${selector} CSS rule not found`);
  return match[1];
}

test('설정 안에서 시작한 드래그를 바깥 클릭으로 오인하지 않는다', () => {
  assert.match(app, /modal\.addEventListener\('pointerdown'/);
  assert.match(app, /startedOnBackdrop:\s*event\.target\s*===\s*modal/);
  assert.match(
    app,
    /startedOnBackdrop\s*&&\s*event\.target\s*===\s*modal/,
    '누르기와 놓기가 모두 backdrop일 때만 닫혀야 한다',
  );
  assert.doesNotMatch(
    app,
    /modal\.addEventListener\('click',[^\n]*e\.target\s*===\s*modal/,
    'click target만 검사하면 안쪽에서 바깥으로 드래그할 때 다시 닫힌다',
  );
});

test('설정 내용만 스크롤되고 저장·취소 푸터는 고정된다', () => {
  const modalRule = rule('.modal');
  assert.match(modalRule, /display\s*:\s*flex/);
  assert.match(modalRule, /flex-direction\s*:\s*column/);
  assert.match(modalRule, /overflow\s*:\s*hidden/);

  const bodyRule = rule('.modal-body');
  assert.match(bodyRule, /flex\s*:\s*1\s+1\s+auto/);
  assert.match(bodyRule, /min-height\s*:\s*0/);
  assert.match(bodyRule, /overflow-y\s*:\s*auto/);

  const footerRule = rule('.modal-foot');
  assert.match(footerRule, /flex\s*:\s*0\s+0\s+auto/);
  assert.match(footerRule, /background\s*:\s*#fff/);
});
