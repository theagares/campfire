'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const source = fs.readFileSync(
  path.join(__dirname, '..', 'content', 'content.js'),
  'utf8',
);

test('failure toast does not misreport a proxy timeout as a panel-open failure', () => {
  assert.doesNotMatch(source, /검토 패널을 열지 못해 (?:업로드|전송)을 멈췄습니다/);
  assert.match(source, /보호 확인을 완료하지 못해 전송을 멈췄습니다/);
  assert.match(source, /보호 확인을 완료하지 못해 업로드를 멈췄습니다/);
});
