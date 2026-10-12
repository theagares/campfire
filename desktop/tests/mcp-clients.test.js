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

function missing(name) {
  return { ok: false, stdout: `No MCP server named "${name}".`, stderr: '' };
}

function stateForPlan(plan, overrides = {}) {
  const transport = plan.args[plan.args.indexOf('--transport') + 1];
  const state = {
    scope: 'User config (available in all your projects)',
    status: '✓ Connected',
    type: transport,
  };
  if (transport === 'http') {
    state.url = plan.args.at(-1);
  } else {
    const separator = plan.args.indexOf('--');
    state.command = plan.args[separator + 1];
    state.args = plan.args.slice(separator + 2).join(' ');
  }
  return { ...state, ...overrides };
}

function renderState(name, state) {
  const lines = [
    `${name}:`,
    `  Scope: ${state.scope}`,
    `  Status: ${state.status}`,
    `  Type: ${state.type}`,
  ];
  if (state.url) lines.push(`  URL: ${state.url}`);
  if (state.command) lines.push(`  Command: ${state.command}`);
  if (state.args !== undefined) lines.push(`  Args: ${state.args}`);
  return lines.join('\n');
}

function claudeHarness(app, initial = {}, options = {}) {
  const plans = _test.buildClaudeCodeAddPlans(app, MCP_URL);
  const planByName = new Map(plans.map(plan => [plan.name, plan]));
  const states = new Map(Object.entries(initial));
  const calls = [];
  const invoke = async (args) => {
    calls.push(args);
    if (args[0] === '--version') return ok('2.1.0');
    if (args[0] !== 'mcp') throw new Error(`unexpected command: ${args.join(' ')}`);
    if (args[1] === 'get') {
      const name = args[2];
      return states.has(name) ? ok(renderState(name, states.get(name))) : missing(name);
    }
    if (args[1] === 'add') {
      const name = args.includes(RISK_SCANNER_SERVER_NAME)
        ? RISK_SCANNER_SERVER_NAME
        : SERVER_NAME;
      if (options.failAdd === name) {
        return { ok: false, stdout: '', stderr: `${name} add failed` };
      }
      states.set(name, stateForPlan(planByName.get(name)));
      return ok();
    }
    if (args[1] === 'remove') {
      const name = args.at(-1);
      if (options.failRemove === name) {
        return { ok: false, stdout: '', stderr: `${name} remove failed` };
      }
      const state = states.get(name);
      if (!state || !/^User config\b/i.test(state.scope)) return missing(name);
      states.delete(name);
      return ok();
    }
    throw new Error(`unexpected command: ${args.join(' ')}`);
  };
  return { calls, invoke, plans, states };
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

test('Claude Code get 결과에서 scope와 실제 health 상태를 함께 판정한다', () => {
  const connected = _test.parseClaudeServer(SERVER_NAME, ok(renderState(SERVER_NAME, {
    scope: 'User config (available in all your projects)',
    status: '✓ Connected',
    type: 'http',
    url: MCP_URL,
  })));
  assert.equal(connected.userScope, true);
  assert.equal(connected.healthy, true);

  const shadowed = _test.parseClaudeServer(SERVER_NAME, ok(renderState(SERVER_NAME, {
    scope: 'Local config (private to you in this project)',
    status: '× Failed to connect',
    type: 'http',
    url: MCP_URL,
  })));
  assert.equal(shadowed.userScope, false);
  assert.equal(shadowed.healthy, false);
  assert.deepEqual(_test.parseClaudeServer('missing', missing('missing')), {
    name: 'missing', exists: false,
  });
});

test('Claude Code health 조회는 기본 MCP 시작 제한보다 길고 변경 명령과 분리된다', () => {
  assert.equal(_test.claudeTimeoutFor(['mcp', 'get', SERVER_NAME], {}), 40000);
  assert.equal(_test.claudeTimeoutFor(['mcp', 'add', SERVER_NAME], {}), 40000);
  assert.equal(_test.claudeTimeoutFor(['mcp', 'remove', SERVER_NAME], {}), 15000);
  assert.equal(_test.claudeTimeoutFor(['--version'], {}), 8000);
  assert.equal(
    _test.claudeTimeoutFor(['mcp', 'get', SERVER_NAME], { MCP_TIMEOUT: '90000' }),
    100000,
  );
});

test('Claude Code 프로세스 생성 예외는 목록 전체를 깨뜨리지 않고 미설치 상태로 내린다', async () => {
  const info = await _test.claudeCodeInfo(
    fakeApp('unused'),
    MCP_URL,
    async () => { throw new Error('spawn denied'); },
  );
  assert.equal(info.available, false);
  assert.equal(info.connected, false);
  assert.match(info.error, /spawn denied/);
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
  const app = fakeApp('unused');
  const plans = _test.buildClaudeCodeAddPlans(app, MCP_URL);
  const harness = claudeHarness(app, {
    [SERVER_NAME]: stateForPlan(plans[0]),
  });

  await _test.claudeCodeConnect(app, MCP_URL, harness.invoke);

  const additions = harness.calls.filter(args => args[1] === 'add');
  assert.equal(additions.length, 1);
  assert.equal(additions[0].includes(RISK_SCANNER_SERVER_NAME), true);
});

test('두 번째 Claude Code 등록 실패 시 이번에 추가한 MCP만 되돌린다', async () => {
  const app = fakeApp('unused');
  const harness = claudeHarness(app, {}, { failAdd: RISK_SCANNER_SERVER_NAME });

  await assert.rejects(
    _test.claudeCodeConnect(app, MCP_URL, harness.invoke),
    /campfire-mcp-risk-scanner add failed/,
  );
  const removals = harness.calls.filter(args => args[1] === 'remove').map(args => args.at(-1));
  assert.deepEqual(removals, [SERVER_NAME]);
});

test('다른 scope의 동명 Claude Code 서버는 연결됨으로 보거나 덮어쓰지 않는다', async () => {
  const app = fakeApp('unused');
  const plans = _test.buildClaudeCodeAddPlans(app, MCP_URL);
  const harness = claudeHarness(app, {
    [SERVER_NAME]: stateForPlan(plans[0], {
      scope: 'Local config (private to you in this project)',
    }),
    [RISK_SCANNER_SERVER_NAME]: stateForPlan(plans[1]),
  });

  const info = await _test.claudeCodeInfo(app, MCP_URL, harness.invoke);
  assert.equal(info.connected, false);
  assert.match(info.error, /다른 scope/);
  await assert.rejects(
    _test.claudeCodeConnect(app, MCP_URL, harness.invoke),
    /먼저 제거/,
  );
  assert.equal(harness.calls.some(args => args[1] === 'add'), false);
  assert.equal(harness.calls.some(args => args[1] === 'remove'), false);
});

test('예전 포트의 Claude Code user 설정은 현재 URL로 교체한다', async () => {
  const app = fakeApp('unused');
  const plans = _test.buildClaudeCodeAddPlans(app, MCP_URL);
  const harness = claudeHarness(app, {
    [SERVER_NAME]: stateForPlan(plans[0], { url: 'http://127.0.0.1:48209/mcp' }),
    [RISK_SCANNER_SERVER_NAME]: stateForPlan(plans[1]),
  });

  const before = await _test.claudeCodeInfo(app, MCP_URL, harness.invoke);
  assert.equal(before.connected, false);
  await _test.claudeCodeConnect(app, MCP_URL, harness.invoke);
  const after = await _test.claudeCodeInfo(app, MCP_URL, harness.invoke);
  assert.equal(after.connected, true);

  const removals = harness.calls.filter(args => args[1] === 'remove').map(args => args.at(-1));
  assert.ok(removals.includes(SERVER_NAME));
  const additions = harness.calls.filter(args => args[1] === 'add');
  assert.equal(additions.length, 1);
  assert.equal(additions[0].at(-1), MCP_URL);
});

test('모양은 같지만 health가 실패한 Claude Code 설정도 전체 값으로 재등록한다', async () => {
  const app = fakeApp('unused');
  const plans = _test.buildClaudeCodeAddPlans(app, MCP_URL);
  const harness = claudeHarness(app, {
    [SERVER_NAME]: stateForPlan(plans[0], { status: '× Failed to connect' }),
    [RISK_SCANNER_SERVER_NAME]: stateForPlan(plans[1]),
  });

  await _test.claudeCodeConnect(app, MCP_URL, harness.invoke);

  const removals = harness.calls.filter(args => args[1] === 'remove').map(args => args.at(-1));
  assert.ok(removals.includes(SERVER_NAME));
  const additions = harness.calls.filter(args => args[1] === 'add');
  assert.equal(additions.length, 1);
  assert.equal(additions[0].at(-1), MCP_URL);
});

test('Claude Code 해제는 일부 실패를 성공으로 숨기지 않고 나머지도 계속 시도한다', async () => {
  const calls = [];
  const invoke = async (args) => {
    calls.push(args);
    const name = args.at(-1);
    if (name === RISK_SCANNER_SERVER_NAME) {
      return { ok: false, stdout: '', stderr: 'scanner remove failed' };
    }
    return name === SERVER_NAME ? ok() : missing(name);
  };

  await assert.rejects(_test.claudeCodeDisconnect(invoke), /scanner remove failed/);
  assert.deepEqual(calls.map(args => args.at(-1)), [
    SERVER_NAME, 'securedoc-gateway', RISK_SCANNER_SERVER_NAME,
  ]);
});

test('Claude Desktop 한 번의 연결/해제가 두 MCP를 함께 관리하고 다른 설정은 보존한다', (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'campfire-mcp-clients-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const app = fakeApp(root);
  const configPath = path.join(root, 'Claude', 'claude_desktop_config.json');
  fs.mkdirSync(path.dirname(configPath), { recursive: true });
  const original = JSON.stringify({
    mcpServers: { existing: { command: 'existing-server' } },
    unrelated: true,
  });
  fs.writeFileSync(configPath, original);

  _test.claudeDesktopConnect(app, MCP_URL);
  const connected = JSON.parse(fs.readFileSync(configPath, 'utf8'));
  assert.deepEqual(connected.mcpServers[SERVER_NAME].args, [
    '-m', 'app.adapters.mcp.stdio_shim',
  ]);
  assert.equal(connected.mcpServers[SERVER_NAME].type, undefined);
  assert.equal(connected.mcpServers[SERVER_NAME].env.SECUREDOC_MCP_URL, MCP_URL);
  assert.equal(connected.mcpServers[RISK_SCANNER_SERVER_NAME].args.at(-1), 'serve');
  assert.equal(connected.mcpServers.existing.command, 'existing-server');
  assert.equal(connected.unrelated, true);
  assert.equal(fs.readFileSync(`${configPath}.campfire-backup`, 'utf8'), original);
  assert.equal(_test.claudeDesktopInfo(app, MCP_URL).connected, true);
  assert.equal(_test.claudeDesktopInfo(app, 'http://127.0.0.1:48209/mcp').connected, false);

  delete connected.mcpServers[RISK_SCANNER_SERVER_NAME];
  fs.writeFileSync(configPath, JSON.stringify(connected));
  assert.equal(
    _test.claudeDesktopInfo(app, MCP_URL).connected,
    false,
    '둘 중 하나만 있으면 연결 완료가 아니다',
  );

  _test.claudeDesktopConnect(app, MCP_URL);
  _test.claudeDesktopDisconnect(app);
  const disconnected = JSON.parse(fs.readFileSync(configPath, 'utf8'));
  assert.equal(disconnected.mcpServers[SERVER_NAME], undefined);
  assert.equal(disconnected.mcpServers[RISK_SCANNER_SERVER_NAME], undefined);
  assert.equal(disconnected.mcpServers.existing.command, 'existing-server');
});

test('손상된 Claude Desktop JSON은 오류로 남기고 원본을 절대 덮어쓰지 않는다', (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'campfire-mcp-invalid-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const app = fakeApp(root);
  const configPath = path.join(root, 'Claude', 'claude_desktop_config.json');
  fs.mkdirSync(path.dirname(configPath), { recursive: true });
  const invalid = '{"mcpServers":';
  fs.writeFileSync(configPath, invalid);

  const info = _test.claudeDesktopInfo(app, MCP_URL);
  assert.equal(info.connected, false);
  assert.match(info.error, /손상/);
  assert.throws(() => _test.claudeDesktopConnect(app, MCP_URL), /손상/);
  assert.equal(fs.readFileSync(configPath, 'utf8'), invalid);
  assert.equal(fs.existsSync(`${configPath}.campfire-backup`), false);
});

test('잘못된 Claude Desktop mcpServers 타입도 덮어쓰지 않는다', (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'campfire-mcp-invalid-shape-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const app = fakeApp(root);
  const configPath = path.join(root, 'Claude', 'claude_desktop_config.json');
  fs.mkdirSync(path.dirname(configPath), { recursive: true });
  const invalid = '{"mcpServers":[],"keep":true}';
  fs.writeFileSync(configPath, invalid);

  const info = _test.claudeDesktopInfo(app, MCP_URL);
  assert.equal(info.connected, false);
  assert.match(info.error, /mcpServers/);
  assert.throws(() => _test.claudeDesktopConnect(app, MCP_URL), /mcpServers/);
  assert.equal(fs.readFileSync(configPath, 'utf8'), invalid);
});

test('Claude Desktop 저장 직전 외부 변경을 감지하면 원본을 보존한다', (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'campfire-mcp-race-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const configPath = path.join(root, 'claude_desktop_config.json');
  fs.writeFileSync(configPath, '{"version":1}');
  const { data, raw } = _test.readJsonDocument(configPath);
  data.version = 2;
  fs.writeFileSync(configPath, '{"version":3}');

  assert.throws(() => _test.writeJsonAtomic(configPath, data, raw), /다른 프로세스/);
  assert.equal(fs.readFileSync(configPath, 'utf8'), '{"version":3}');
});

test('수동 연결 스니펫에도 두 MCP가 함께 들어간다', () => {
  for (const client of _test.manualClients(fakeApp('unused'), MCP_URL)) {
    const parsed = JSON.parse(client.snippet);
    const servers = parsed.mcpServers || parsed.servers;
    assert.ok(servers[SERVER_NAME], client.name);
    assert.ok(servers[RISK_SCANNER_SERVER_NAME], client.name);
  }
});
