'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const { isLite, liteSettingsPatch } = require('../main/edition');

test('버전에 -lite 가 붙은 빌드만 라이트 판이다', () => {
  assert.equal(isLite('0.2.34-lite.1', {}), true);
  assert.equal(isLite('0.2.34-lite', {}), true);
  assert.equal(isLite('0.2.34', {}), false);
  assert.equal(isLite('0.2.34-beta.1', {}), false);
  assert.equal(isLite('0.2.34-lightning.1', {}), false); // 앞부분만 같은 다른 채널
  assert.equal(isLite('0.2.34', { CAMPFIRE_EDITION: 'lite' }), true); // 개발 중 확인용
});

test('라이트 판은 기밀·MCP·프록시 설정을 저장하지 않고 나머지는 그대로 둔다', () => {
  const patch = {
    confidentialEnabled: true,
    kgPath: 'C:/kg.jsonl',
    mcpRiskScannerEnabled: true,
    proxyEnabled: true,
    upstageApiKey: 'up_test',
    injectionPolicy: 'block',
  };
  assert.deepEqual(liteSettingsPatch(patch), { upstageApiKey: 'up_test', injectionPolicy: 'block' });
  assert.deepEqual(liteSettingsPatch(undefined), {});
});
