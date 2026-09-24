'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');

const { ConfigStore } = require('../main/config-store');
const packageJson = require('../package.json');

test('MCP 위험 검사기는 기본 OFF이고 설정에 명시적으로 보존된다', (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'campfire-mcp-risk-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));

  const config = new ConfigStore(root);
  assert.equal(config.get('mcpRiskScannerEnabled'), false);
  config.set({ mcpRiskScannerEnabled: true });
  assert.equal(new ConfigStore(root).get('mcpRiskScannerEnabled'), true);
});

test('배포 리소스에서 검사기 구현은 엔진과 별도 항목이다', () => {
  const resource = packageJson.build.extraResources.find(
    (item) => item.from === '../experiments/mcp_risk_scanner'
  );
  assert.ok(resource);
  assert.equal(resource.to, 'engine/mcp_risk_scanner');
  assert.ok(resource.filter.includes('!tests/**'));
});
