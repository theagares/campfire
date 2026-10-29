'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const renderer = path.join(__dirname, '..', 'renderer');
const html = fs.readFileSync(path.join(renderer, 'index.html'), 'utf8');
const app = fs.readFileSync(path.join(renderer, 'app.js'), 'utf8');

function position(id) {
  const at = html.indexOf(`id="${id}"`);
  assert.notEqual(at, -1, `${id}가 대시보드에 없다`);
  return at;
}

test('대시보드 블록은 모델·탐지량·선택 모델·선택 탐지량·리소스 순서다', () => {
  const ids = [
    'home-model-pii-card', 'home-model-inj-card',
    'tile-pii-today', 'tile-inj-today',
    'dash-conf-card', 'dash-image-card',
    'dash-conf-detect-card', 'dash-document-detect-card',
    'db-resources',
  ];
  const positions = ids.map(position);
  assert.deepEqual(positions, [...positions].sort((a, b) => a - b));
});

test('기밀이 꺼지면 VARCO와 문서 탐지량이 각 줄의 왼쪽 첫 카드가 된다', () => {
  assert.match(html, /id="dash-conf-card" style="display:none"[\s\S]*id="dash-image-card"/);
  assert.match(html, /id="dash-conf-detect-card" style="display:none"[\s\S]*id="dash-document-detect-card"/);
  assert.match(app, /dash-conf-card'\)\.style\.display = confShown \? '' : 'none'/);
  assert.match(app, /dash-conf-detect-card'\)\.style\.display = confShown \? '' : 'none'/);
});

test('라이트 판은 VARCO 모델과 문서 탐지량을 함께 숨긴다', () => {
  assert.match(app, /const imageShown = !state\.lite/);
  assert.match(app, /dash-image-card'\)\.style\.display = imageShown/);
  assert.match(app, /dash-document-detect-card'\)\.style\.display = imageShown/);
});
