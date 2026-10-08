'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');

const {
  SERVER_NAME,
  RISK_SCANNER_SERVER_NAME,
  _test,
} = require('../main/mcp-clients');

const MCP_URL = 'http://127.0.0.1:48200/mcp';

function fakeApp(appData, isPackaged = false) {
  return {
    isPackaged,
    getPath(name) {
      assert.equal(name, 'appData');
      return appData;
    },
  };
}

function ok(stdout = '') {
  return { ok: true, stdout, stderr: '' };
}

test('Windows npm shim은 셸 대신 그 안의 공식 Claude 실행 파일로 해석한다', (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'campfire-claude-shim-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const shim = path.join(root, 'claude.cmd');
  const native = path.join(
    root, 'node_modules', '@anthropic-ai', 'claude-code', 'bin', 'claude.exe',
  );
  fs.mkdirSync(path.dirname(native), { recursive: true });
  fs.writeFileSync(shim, '@echo off\n');
  fs.writeFileSync(native, 'placeholder');

  assert.deepEqual(_test.resolveClaudeLauncherInPath(root, 'win32'), {
    command: native,
    prefixArgs: [],
  });
});

test('위험 검사기만 있는 목록을 메인 MCP 연결로 오판하지 않는다', () => {
  const output = `${RISK_SCANNER_SERVER_NAME}: python -m scanner - Connected\n`;
  assert.equal(_test.listedServer(output, SERVER_NAME), false);
  assert.equal(_test.listedServer(output, RISK_SCANNER_SERVER_NAME), true);
});

test('Claude Code 등록 계획은 HTTP MCP와 분리형 stdio 검사기를 함께 담는다', () => {
  const plans = _test.buildClaudeCodeAddPlans(fakeApp('unused'), MCP_URL);
  assert.deepEqual(plans.map(plan => plan.name), [SERVER_NAME, RISK_SCANNER_SERVER_NAME]);
  assert.deepEqual(plans[0].args, [
    'mcp', 'add', '--transport', 'http', '--scope', 'user', SERVER_NAME, MCP_URL,
  ]);

  const scannerArgs = plans[1].args;
  assert.equal(scannerArgs.includes('--transport'), true);
  assert.equal(scannerArgs.includes('stdio'), true);
  assert.equal(scannerArgs.includes(RISK_SCANNER_SERVER_NAME), true);
  assert.equal(scannerArgs.includes('experiments.mcp_risk_scanner.cli'), true);
  assert.equal(scannerArgs.includes('serve'), true);
  assert.equal(scannerArgs.some(arg => arg.startsWith('PYTHONPATH=')), true);
  assert.equal(scannerArgs.some(arg => arg.startsWith('PYTHONPYCACHEPREFIX=')), true);
});

test('기존 campfire 연결에는 빠진 위험 검사기만 추가한다', async () => {
  const calls = [];
  const invoke = async (args) => {
    calls.push(args);
    if (args[0] === 'mcp' && args[1] === 'list') {
      return ok(`${SERVER_NAME}: ${MCP_URL} (HTTP) - Connected\n`);
    }
    return ok();
  };

  await _test.claudeCodeConnect(fakeApp('unused'), MCP_URL, invoke);

  assert.equal(calls.length, 2);
  assert.deepEqual(calls[0], ['mcp', 'list']);
  assert.equal(calls[1].includes(RISK_SCANNER_SERVER_NAME), true);
  assert.equal(calls[1].includes(SERVER_NAME) && !calls[1].includes(RISK_SCANNER_SERVER_NAME), false);
});

test('두 번째 Claude Code 등록 실패 시 이번에 추가한 MCP만 되돌린다', async () => {
  const calls = [];
  const invoke = async (args) => {
    calls.push(args);
    if (args[0] === 'mcp' && args[1] === 'list') return ok('');
    if (args.includes(RISK_SCANNER_SERVER_NAME) && args[1] === 'add') {
      return { ok: false, stdout: '', stderr: 'scanner failed' };
    }
    return ok();
  };

  await assert.rejects(
    _test.claudeCodeConnect(fakeApp('unused'), MCP_URL, invoke),
    /scanner failed/,
  );
  const removals = calls.filter(args => args[1] === 'remove').map(args => args.at(-1));
  assert.deepEqual(removals, [SERVER_NAME, RISK_SCANNER_SERVER_NAME]);
});

test('Claude Desktop 한 번의 연결/해제가 두 MCP를 함께 관리하고 다른 설정은 보존한다', (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'campfire-mcp-clients-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const app = fakeApp(root);
  const configPath = path.join(root, 'Claude', 'claude_desktop_config.json');
  fs.mkdirSync(path.dirname(configPath), { recursive: true });
  fs.writeFileSync(configPath, JSON.stringify({
    mcpServers: { existing: { command: 'existing-server' } },
    unrelated: true,
  }));

  _test.claudeDesktopConnect(app, MCP_URL);
  const connected = JSON.parse(fs.readFileSync(configPath, 'utf8'));
  assert.equal(connected.mcpServers[SERVER_NAME].url, MCP_URL);
  assert.equal(connected.mcpServers[RISK_SCANNER_SERVER_NAME].args.at(-1), 'serve');
  assert.equal(connected.mcpServers.existing.command, 'existing-server');
  assert.equal(connected.unrelated, true);
  assert.equal(_test.claudeDesktopInfo(app).connected, true);

  delete connected.mcpServers[RISK_SCANNER_SERVER_NAME];
  fs.writeFileSync(configPath, JSON.stringify(connected));
  assert.equal(_test.claudeDesktopInfo(app).connected, false, '둘 중 하나만 있으면 연결 완료가 아니다');

  _test.claudeDesktopConnect(app, MCP_URL);
  _test.claudeDesktopDisconnect(app);
  const disconnected = JSON.parse(fs.readFileSync(configPath, 'utf8'));
  assert.equal(disconnected.mcpServers[SERVER_NAME], undefined);
  assert.equal(disconnected.mcpServers[RISK_SCANNER_SERVER_NAME], undefined);
  assert.equal(disconnected.mcpServers.existing.command, 'existing-server');
});

test('수동 연결 스니펫에도 두 MCP가 함께 들어간다', () => {
  for (const client of _test.manualClients(fakeApp('unused'), MCP_URL)) {
    const parsed = JSON.parse(client.snippet);
    const servers = parsed.mcpServers || parsed.servers;
    assert.ok(servers[SERVER_NAME], client.name);
    assert.ok(servers[RISK_SCANNER_SERVER_NAME], client.name);
  }
});
