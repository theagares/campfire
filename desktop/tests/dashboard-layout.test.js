'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const renderer = path.join(__dirname, '..', 'renderer');
const html = fs.readFileSync(path.join(renderer, 'index.html'), 'utf8');
const app = fs.readFileSync(path.join(renderer, 'app.js'), 'utf8');
const styles = fs.readFileSync(path.join(renderer, 'styles.css'), 'utf8');

function position(id) {
  const at = html.indexOf(`id="${id}"`);
  assert.notEqual(at, -1, `${id}가 대시보드에 없다`);
  return at;
}

test('대시보드 블록은 페어형 grid가 지정할 수 있는 고유 ID와 단일 래퍼를 가진다', () => {
  const ids = [
    'home-model-pii-card', 'home-model-inj-card',
    'dash-pii-detect-card', 'dash-inj-detect-card',
    'dash-conf-card', 'dash-image-card',
    'dash-conf-detect-card', 'dash-document-detect-card',
    'dash-resource-card',
  ];
  const positions = ids.map(position);
  assert.deepEqual(positions, [...positions].sort((a, b) => a - b));
  assert.match(html, /id="dashboard-grid"[\s\S]*id="dash-resource-card"/);
});

test('시안 v3 2번처럼 모델과 해당 탐지량을 이웃 칸에 둔다', () => {
  assert.match(styles, /grid-template-areas:\s*"m1 d1 m2 d2"\s*"m3 d3 m4 d4"\s*"res res res res"/);
  assert.match(styles, /#home-model-pii-card\s*\{\s*grid-area:m1/);
  assert.match(styles, /#dash-pii-detect-card\s*\{\s*grid-area:d1/);
  assert.match(styles, /#home-model-inj-card\s*\{\s*grid-area:m2/);
  assert.match(styles, /#dash-inj-detect-card\s*\{\s*grid-area:d2/);
});

test('기밀 OFF는 시안과 같은 3열 7블록 배치로 바뀐다', () => {
  assert.match(styles, /#dashboard-grid\.conf-off[\s\S]*"m1 m2 m4"\s*"d1 d2 d4"\s*"res res res"/);
  assert.match(app, /dashboardGrid\.classList\.toggle\('conf-off', !confShown\)/);
  assert.match(app, /dash-conf-card'\)\.style\.display = confShown \? '' : 'none'/);
  assert.match(app, /dash-conf-detect-card'\)\.style\.display = confShown \? '' : 'none'/);
});

test('라이트 판은 VARCO 모델과 문서 탐지량을 함께 숨긴다', () => {
  assert.match(app, /const imageShown = !state\.lite/);
  assert.match(app, /dashboardGrid\.classList\.toggle\('lite', state\.lite\)/);
  assert.match(app, /dash-image-card'\)\.style\.display = imageShown/);
  assert.match(app, /dash-document-detect-card'\)\.style\.display = imageShown/);
  assert.match(styles, /#dashboard-grid\.lite[\s\S]*"m1 m2 m3"\s*"d1 d2 d3"\s*"res res res"/);
});

test('VARCO 테두리 그림은 PNG 원본 비율로 표시한다', () => {
  const png = path.join(__dirname, '..', 'assets', 'figma', 'dash-varco-model.png');
  const oldSvg = path.join(__dirname, '..', 'assets', 'figma', 'dash-varco-model.svg');
  assert.match(html, /dash-varco-model\.png/);
  assert.doesNotMatch(html, /dash-varco-model\.svg/);
  assert.equal(fs.readFileSync(png).subarray(0, 8).toString('hex'), '89504e470d0a1a0a');
  assert.equal(fs.existsSync(oldSvg), false);
  assert.match(styles, /\.model-card\.image \.mc-photo \{[^}]*border-radius:0;[^}]*object-fit:contain;/);
});

test('시스템 리소스는 원래 칸 비율을 유지한 네 칸 한 줄이다', () => {
  assert.match(styles, /#dashboard-grid \.res-cols \{[\s\S]*grid-template-columns:repeat\(4,minmax\(0,206px\)\)/);
  assert.match(styles, /#dashboard-grid \.res-block \.rb-icon \{ width:29px; height:29px;/);
  assert.match(styles, /#dashboard-grid \.res-block \.rb-body \{ padding-left:39px;/);
});

test('최소 창은 시안의 0.735 배율로 본문 전체를 축소한다', () => {
  assert.match(styles, /@media \(max-width:1000px\)[\s\S]*\.dashboard-col \{ width:880px; max-width:none; zoom:\.735;/);
});
