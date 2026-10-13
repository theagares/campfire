'use strict';
/**
 * main/mcp-clients.js
 * "연결" 화면에서 원클릭으로 Campfire MCP와 분리형 위험 검사기 MCP를 각 AI
 * 클라이언트에 함께 등록/해제한다.
 *
 * Claude Code / Claude Desktop 은 실제로 자동 등록까지 수행한다:
 *   - Claude Code: 공식 CLI(`claude mcp add/remove`)를 그대로 호출 — 내부 설정 파일
 *     포맷/경로를 추측하지 않고 공식 인터페이스에 위임한다(이번 세션에서 실측 검증됨).
 *   - Claude Desktop: 설정 파일이 지원하는 로컬 stdio 서버로 등록하고, 번들된
 *     stdio_shim 이 실행 중인 Campfire HTTP MCP로 중계한다. HTTP 서버를 설정 파일에
 *     직접 넣으면 Claude Desktop이 무시하므로 사용하지 않는다.
 * 나머지(Cursor/Windsurf/Cline/VS Code Copilot)는 클라이언트마다 MCP 설정 스키마·경로가
 * 자주 바뀌고 있어, 잘못 자동으로 써버리면 사용자의 기존 설정을 조용히 깨뜨릴 위험이
 * 있다 — 그래서 붙여넣을 JSON 스니펫만 제공한다(수동, method:'manual').
 */

const { execFile } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const appPaths = require('./paths');

const SERVER_NAME = 'campfire';
const RISK_SCANNER_SERVER_NAME = 'campfire-mcp-risk-scanner';
const CLAUDE_SHORT_TIMEOUT_MS = 8000;
const CLAUDE_MUTATION_TIMEOUT_MS = 15000;
// Claude Code의 MCP 시작 기본 제한은 30초다. get은 해당 서버를 실제 health-check하므로
// 그보다 짧게 자르면 정상 설정도 실패로 오판한다.
const CLAUDE_HEALTH_TIMEOUT_MS = 40000;
// 리브랜딩 이전에 등록해둔 키. 사용자의 claude_desktop_config.json 에 그대로 남아 있어서,
// 새 키만 보면 "연결 안 됨" 으로 보이고 해제해도 옛 항목이 계속 남는다. 조회·해제 때
// 둘 다 취급하고, 연결할 때는 옛 항목을 지우고 새 키로 바꿔 쓴다.
const LEGACY_SERVER_NAMES = ['securedoc-gateway'];
const mainServerKeysIn = (servers) =>
  [SERVER_NAME, ...LEGACY_SERVER_NAMES].filter(k => servers && servers[k]);
const managedServerKeysIn = (servers) =>
  [...mainServerKeysIn(servers), RISK_SCANNER_SERVER_NAME].filter(k => servers && servers[k]);

/** CLI 를 찾을 수 있는 PATH 를 만든다(한 번만 계산해 캐시).
 *
 *  macOS/Linux 에서 Finder·Dock·LaunchServices 로 앱을 켜면 로그인 셸을 거치지 않아
 *  PATH 가 사실상 /usr/bin:/bin:/usr/sbin:/sbin 뿐이다. Claude Code 는 ~/.local/bin
 *  이나 /opt/homebrew/bin 같은 곳에 설치되므로, 그 상태로 `claude --version` 을 부르면
 *  설치돼 있는데도 "미설치" 로 잡힌다(실사용자 macOS 리포트). 터미널에서 앱을 실행하면
 *  셸 PATH 를 물려받아 잘 되기 때문에 개발 중엔 잘 드러나지 않는 문제다.
 *
 *  로그인 셸에 PATH 를 직접 물어보고(사용자가 nvm/asdf/volta 로 잡아둔 경로까지 반영),
 *  셸이 느리거나 실패해도 흔한 설치 위치는 따로 얹어 최소한을 보장한다.
 */
let _pathPromise = null;
let _claudeLauncherPromise = null;
function resolveCliPath() {
  if (_pathPromise) return _pathPromise;
  _pathPromise = (async () => {
    const parts = (process.env.PATH || '').split(path.delimiter).filter(Boolean);
    const add = (p) => { if (p && !parts.includes(p)) parts.push(p); };
    if (process.platform === 'win32') return parts.join(path.delimiter);

    const shell = process.env.SHELL;
    if (shell) {
      // -lc: 로그인 셸로 rc 를 읽되 대화형(-i)은 피한다 — 대화형은 프롬프트 설정에서
      // 멈춰 앱 기동이 지연될 수 있다. 실패/타임아웃은 조용히 넘어간다.
      const shellPath = await new Promise((resolve) => {
        try {
          execFile(shell, ['-lc', 'printf %s "$PATH"'], { timeout: 3000 }, (err, stdout) => {
            resolve(err ? '' : String(stdout || ''));
          });
        } catch {
          resolve('');
        }
      });
      shellPath.split(path.delimiter).filter(Boolean).forEach(add);
    }

    const home = os.homedir();
    for (const p of [
      '/opt/homebrew/bin',                    // Apple Silicon Homebrew
      '/usr/local/bin',                       // Intel Homebrew / npm 기본 prefix
      path.join(home, '.local', 'bin'),       // Claude Code 네이티브 설치
      path.join(home, '.bun', 'bin'),
      path.join(home, '.volta', 'bin'),
      path.join(home, '.npm-global', 'bin'),
      path.join(home, '.nvm', 'current', 'bin'),
    ]) add(p);

    return parts.join(path.delimiter);
  })();
  return _pathPromise;
}

function firstFile(searchPath, names) {
  for (const rawDir of String(searchPath || '').split(path.delimiter).filter(Boolean)) {
    const dir = rawDir.replace(/^"|"$/g, '');
    for (const name of names) {
      const candidate = path.join(dir, name);
      try {
        if (fs.statSync(candidate).isFile()) return candidate;
      } catch {
        // 다음 후보를 찾는다.
      }
    }
  }
  return null;
}

/** 셸 없이 실행할 Claude Code 엔트리를 찾는다.
 *
 * Windows npm 전역 설치는 `claude.cmd`만 PATH에 두지만 그 shim을 execFile로 직접
 * 실행할 수는 없다. shell:true로 우회하면 설치 경로와 인자가 셸 코드가 될 수 있으므로,
 * shim 옆 공식 네이티브 바이너리(신형) 또는 Node 엔트리(구형)를 직접 실행한다.
 */
function resolveClaudeLauncherInPath(searchPath, platform = process.platform) {
  if (platform !== 'win32') {
    const command = firstFile(searchPath, ['claude']);
    return command ? { command, prefixArgs: [] } : null;
  }

  const native = firstFile(searchPath, ['claude.exe']);
  if (native) return { command: native, prefixArgs: [] };

  const shim = firstFile(searchPath, ['claude.cmd', 'claude.ps1']);
  if (!shim) return null;
  const shimDir = path.dirname(shim);
  const bundledNative = path.join(
    shimDir, 'node_modules', '@anthropic-ai', 'claude-code', 'bin', 'claude.exe',
  );
  if (fs.existsSync(bundledNative)) return { command: bundledNative, prefixArgs: [] };

  const cliEntrypoints = [
    path.join(shimDir, 'node_modules', '@anthropic-ai', 'claude-code', 'cli.js'),
    path.join(shimDir, 'node_modules', '@anthropic-ai', 'claude-code', 'bin', 'claude.js'),
  ];
  const cli = cliEntrypoints.find(candidate => fs.existsSync(candidate));
  const node = firstFile(searchPath, ['node.exe']);
  return cli && node ? { command: node, prefixArgs: [cli] } : null;
}

async function resolveClaudeLauncher() {
  if (_claudeLauncherPromise) return _claudeLauncherPromise;
  _claudeLauncherPromise = resolveCliPath().then((PATH) => (
    resolveClaudeLauncherInPath(PATH) || { command: 'claude', prefixArgs: [] }
  ));
  return _claudeLauncherPromise;
}

function claudeTimeoutFor(args, env = process.env) {
  if (args[0] === 'mcp' && ['get', 'add'].includes(args[1])) {
    const configured = Number(env.MCP_TIMEOUT);
    return Number.isFinite(configured) && configured > 0
      ? Math.max(CLAUDE_HEALTH_TIMEOUT_MS, Math.min(configured + 10000, 300000))
      : CLAUDE_HEALTH_TIMEOUT_MS;
  }
  if (args[0] === 'mcp' && args[1] === 'remove') {
    return CLAUDE_MUTATION_TIMEOUT_MS;
  }
  return CLAUDE_SHORT_TIMEOUT_MS;
}

async function runClaude(args) {
  const PATH = await resolveCliPath();
  const launcher = await resolveClaudeLauncher();
  return new Promise((resolve) => {
    const finish = (err, stdout = '', stderr = '') => resolve({
      ok: !err,
      stdout: stdout || '',
      stderr: stderr || (err && err.message) || '',
      code: err && err.code,
      timedOut: !!(err && err.killed),
    });
    try {
      execFile(
        launcher.command,
        [...launcher.prefixArgs, ...args],
        { timeout: claudeTimeoutFor(args), windowsHide: true, env: { ...process.env, PATH } },
        finish,
      );
    } catch (err) {
      finish(err);
    }
  });
}

function resultText(result) {
  return [result && result.stdout, result && result.stderr].filter(Boolean).join('\n').trim();
}

function isMissingClaudeServer(result) {
  return !result.ok && /No MCP server (?:named|found)/i.test(resultText(result));
}

function outputField(output, label) {
  const prefix = `${label}:`;
  const line = String(output || '').split(/\r?\n/)
    .find(candidate => candidate.trimStart().startsWith(prefix));
  return line ? line.trimStart().slice(prefix.length).trim() : '';
}

function parseClaudeServer(name, result) {
  if (isMissingClaudeServer(result)) return { name, exists: false };
  if (!result.ok) {
    const suffix = result.timedOut ? ' (시간 초과)' : '';
    throw new Error(resultText(result) || `claude mcp get ${name} 실행 실패${suffix}`);
  }
  const output = resultText(result);
  const status = outputField(output, 'Status');
  const scope = outputField(output, 'Scope');
  return {
    name,
    exists: true,
    scope,
    userScope: /^User config\b/i.test(scope),
    healthy: /\bConnected\b/i.test(status) && !/\b(?:Failed|Disconnected)\b/i.test(status),
    type: outputField(output, 'Type').toLowerCase(),
    url: outputField(output, 'URL'),
    command: outputField(output, 'Command'),
    args: outputField(output, 'Args'),
    status,
  };
}

async function inspectClaudeServer(name, invoke = runClaude) {
  return parseClaudeServer(name, await invoke(['mcp', 'get', name]));
}

function buildRiskScannerLaunch(app) {
  const engineDir = appPaths.resolveEngineDir(app);
  const packaged = !!(app && app.isPackaged);
  return {
    command: appPaths.resolvePythonExe(engineDir),
    args: ['-m', packaged ? 'mcp_risk_scanner.cli' : 'experiments.mcp_risk_scanner.cli', 'serve'],
    env: {
      PYTHONPATH: packaged ? engineDir : path.resolve(engineDir, '..'),
      PYTHONUTF8: '1',
      PYTHONPYCACHEPREFIX: appPaths.resolvePycacheDir(),
    },
  };
}

function buildMainBridgeLaunch(app, mcpUrl) {
  const engineDir = appPaths.resolveEngineDir(app);
  return {
    command: appPaths.resolvePythonExe(engineDir),
    args: ['-m', 'app.adapters.mcp.stdio_shim'],
    env: {
      PYTHONPATH: engineDir,
      PYTHONUTF8: '1',
      PYTHONPYCACHEPREFIX: appPaths.resolvePycacheDir(),
      SECUREDOC_MCP_URL: mcpUrl,
    },
  };
}

function buildClaudeCodeAddPlans(app, mcpUrl) {
  const scanner = buildRiskScannerLaunch(app);
  const scannerEnv = Object.entries(scanner.env).flatMap(([key, value]) => [`${key}=${value}`]);
  return [
    {
      name: SERVER_NAME,
      args: ['mcp', 'add', '--transport', 'http', '--scope', 'user', SERVER_NAME, mcpUrl],
    },
    {
      name: RISK_SCANNER_SERVER_NAME,
      args: [
        'mcp', 'add', '--transport', 'stdio', '--scope', 'user',
        RISK_SCANNER_SERVER_NAME, '-e', ...scannerEnv, '--', scanner.command, ...scanner.args,
      ],
    },
  ];
}

function sameCommand(left, right) {
  if (!left || !right) return false;
  if (process.platform === 'win32') return left.toLowerCase() === right.toLowerCase();
  return left === right;
}

function claudeStateMatchesPlan(state, plan) {
  if (!state.exists || !state.userScope) return false;
  const transportIndex = plan.args.indexOf('--transport');
  const transport = transportIndex >= 0 ? plan.args[transportIndex + 1] : 'stdio';
  if (state.type !== transport) return false;
  if (transport === 'http') {
    return state.url === plan.args.at(-1);
  }
  const separator = plan.args.indexOf('--');
  const command = separator >= 0 ? plan.args[separator + 1] : '';
  const args = separator >= 0 ? plan.args.slice(separator + 2) : [];
  return sameCommand(state.command, command)
    && state.args.replace(/\s+/g, ' ').trim() === args.join(' ');
}

function claudeStateReady(state, plan) {
  return claudeStateMatchesPlan(state, plan) && state.healthy;
}

async function claudeCodeInfo(app, mcpUrl, invoke = runClaude) {
  let ver;
  try {
    ver = await invoke(['--version']);
  } catch (err) {
    return {
      id: 'claude_code', name: 'Claude Code', method: 'cli', available: false, connected: false,
      error: err.message || String(err),
    };
  }
  if (!ver.ok) {
    return {
      id: 'claude_code', name: 'Claude Code', method: 'cli', available: false, connected: false,
      error: resultText(ver),
    };
  }
  try {
    const plans = buildClaudeCodeAddPlans(app, mcpUrl);
    const states = await Promise.all(plans.map(plan => inspectClaudeServer(plan.name, invoke)));
    const connected = states.every((state, index) => claudeStateReady(state, plans[index]));
    let error = '';
    const conflicting = states.find(state => state.exists && !state.userScope);
    const unhealthy = states.find((state, index) => (
      claudeStateMatchesPlan(state, plans[index]) && !state.healthy
    ));
    if (conflicting) error = `${conflicting.name}이 user가 아닌 다른 scope에서 우선 적용 중입니다.`;
    else if (unhealthy) error = `${unhealthy.name} 설정은 맞지만 연결 상태가 ${unhealthy.status || '비정상'}입니다.`;
    return {
      id: 'claude_code', name: 'Claude Code', method: 'cli', available: true, connected, error,
    };
  } catch (err) {
    return {
      id: 'claude_code', name: 'Claude Code', method: 'cli', available: true, connected: false,
      error: err.message || String(err),
    };
  }
}

async function claudeCodeConnect(app, mcpUrl, invoke = runClaude) {
  const plans = buildClaudeCodeAddPlans(app, mcpUrl);
  const states = await Promise.all(plans.map(plan => inspectClaudeServer(plan.name, invoke)));
  const conflicting = states.find(state => state.exists && !state.userScope);
  if (conflicting) {
    throw new Error(
      `${conflicting.name}이 ${conflicting.scope || '다른 scope'}에 있어 user 설정을 가립니다. `
      + '해당 scope의 동명 서버를 먼저 제거하세요.',
    );
  }

  const added = [];

  for (let index = 0; index < plans.length; index += 1) {
    const plan = plans[index];
    const state = states[index];
    if (claudeStateReady(state, plan)) continue;
    // 명령/URL이 같아 보여도 get 출력에 감춰진 env가 오래됐을 수 있다. health가
    // 실패한 user 설정은 같은 값으로 단정하지 말고 제거 후 현재 전체 설정으로 복구한다.
    if (state.exists) {
      const removed = await invoke(['mcp', 'remove', '--scope', 'user', plan.name]);
      if (!removed.ok && !isMissingClaudeServer(removed)) {
        throw new Error(resultText(removed) || `claude mcp remove ${plan.name} 실행 실패`);
      }
    }
    const res = await invoke(plan.args);
    if (!res.ok) {
      const rollbackFailures = [];
      // 성공이 확인된 add만 되돌린다. 실패한 plan 이름까지 지우면 동시에 만들어진
      // 설정이나 기존 설정을 삭제할 수 있다.
      for (const name of [...added].reverse()) {
        const rollback = await invoke(['mcp', 'remove', '--scope', 'user', name]);
        if (!rollback.ok && !isMissingClaudeServer(rollback)) {
          rollbackFailures.push(`${name}: ${resultText(rollback) || 'remove 실패'}`);
        }
      }
      const rollbackMessage = rollbackFailures.length
        ? `; 롤백 실패: ${rollbackFailures.join(', ')}`
        : '';
      throw new Error((resultText(res) || `claude mcp add ${plan.name} 실행 실패`) + rollbackMessage);
    }
    added.push(plan.name);
  }

  const verified = await Promise.all(plans.map(plan => inspectClaudeServer(plan.name, invoke)));
  const badIndex = verified.findIndex((state, index) => !claudeStateReady(state, plans[index]));
  if (badIndex >= 0) {
    const rollbackFailures = [];
    for (const name of [...added].reverse()) {
      const rollback = await invoke(['mcp', 'remove', '--scope', 'user', name]);
      if (!rollback.ok && !isMissingClaudeServer(rollback)) {
        rollbackFailures.push(`${name}: ${resultText(rollback) || 'remove 실패'}`);
      }
    }
    const state = verified[badIndex];
    const rollbackMessage = rollbackFailures.length
      ? `; 롤백 실패: ${rollbackFailures.join(', ')}`
      : '';
    throw new Error(`${plans[badIndex].name} 등록 검증 실패: ${state.status || '설정 불일치'}${rollbackMessage}`);
  }

  // 새 이름이 모두 정상인 뒤에만 user-scope 레거시 이름을 정리한다.
  for (const name of LEGACY_SERVER_NAMES) {
    const legacy = await invoke(['mcp', 'remove', '--scope', 'user', name]);
    if (!legacy.ok && !isMissingClaudeServer(legacy)) {
      throw new Error(resultText(legacy) || `claude mcp remove ${name} 실행 실패`);
    }
  }
}

async function claudeCodeDisconnect(invoke = runClaude) {
  const failures = [];
  for (const key of [SERVER_NAME, ...LEGACY_SERVER_NAMES, RISK_SCANNER_SERVER_NAME]) {
    const result = await invoke(['mcp', 'remove', '--scope', 'user', key]);
    if (!result.ok && !isMissingClaudeServer(result)) {
      failures.push(`${key}: ${resultText(result) || 'remove 실패'}`);
    }
  }
  if (failures.length) {
    throw new Error(`일부 Claude Code MCP를 제거하지 못했습니다: ${failures.join(', ')}`);
  }
}

function claudeDesktopConfigPath(app) {
  return path.join(app.getPath('appData'), 'Claude', 'claude_desktop_config.json');
}

function readJsonDocument(p) {
  if (!fs.existsSync(p)) return { data: {}, raw: null };
  let raw;
  try {
    raw = fs.readFileSync(p, 'utf-8');
  } catch (err) {
    throw new Error(`Claude Desktop 설정 파일을 읽을 수 없습니다: ${err.message}`);
  }
  let data;
  try {
    data = JSON.parse(raw);
  } catch (err) {
    throw new Error(`Claude Desktop 설정 JSON이 손상되어 변경하지 않았습니다: ${err.message}`);
  }
  if (!data || typeof data !== 'object' || Array.isArray(data)) {
    throw new Error('Claude Desktop 설정 JSON의 최상위 값이 객체가 아니어서 변경하지 않았습니다.');
  }
  return { data, raw };
}

function writeJsonAtomic(p, data, expectedRaw) {
  const dir = path.dirname(p);
  fs.mkdirSync(dir, { recursive: true });
  const currentRaw = fs.existsSync(p) ? fs.readFileSync(p, 'utf-8') : null;
  if (currentRaw !== expectedRaw) {
    throw new Error('Claude Desktop 설정이 다른 프로세스에서 변경되어 저장을 중단했습니다. 다시 시도하세요.');
  }

  const temp = path.join(
    dir,
    `.${path.basename(p)}.campfire-${process.pid}-${Date.now()}-${Math.random().toString(16).slice(2)}.tmp`,
  );
  const backup = `${p}.campfire-backup`;
  let fd = null;
  try {
    const mode = fs.existsSync(p) ? fs.statSync(p).mode : 0o600;
    fd = fs.openSync(temp, 'wx', mode);
    fs.writeFileSync(fd, JSON.stringify(data, null, 2) + '\n', 'utf-8');
    fs.fsyncSync(fd);
    fs.closeSync(fd);
    fd = null;

    const latestRaw = fs.existsSync(p) ? fs.readFileSync(p, 'utf-8') : null;
    if (latestRaw !== expectedRaw) {
      throw new Error('Claude Desktop 설정이 저장 도중 변경되어 덮어쓰지 않았습니다. 다시 시도하세요.');
    }
    if (latestRaw !== null) fs.copyFileSync(p, backup);
    fs.renameSync(temp, p);
  } catch (err) {
    if (fd !== null) {
      try { fs.closeSync(fd); } catch { /* 이미 닫힘 */ }
    }
    try { fs.rmSync(temp, { force: true }); } catch { /* 임시 파일 정리 실패는 원래 오류를 보존 */ }
    throw err;
  }
}

function mcpServersIn(data, { create = false } = {}) {
  if (data.mcpServers === undefined && create) {
    data.mcpServers = {};
    return data.mcpServers;
  }
  if (
    data.mcpServers
    && typeof data.mcpServers === 'object'
    && !Array.isArray(data.mcpServers)
  ) {
    return data.mcpServers;
  }
  if (data.mcpServers === undefined) return undefined;
  throw new Error('Claude Desktop 설정의 mcpServers가 객체가 아니어서 변경하지 않았습니다.');
}

function sameLaunch(actual, expected) {
  if (!actual || typeof actual !== 'object' || Array.isArray(actual)) return false;
  if (!sameCommand(actual.command, expected.command)) return false;
  if (!Array.isArray(actual.args) || actual.args.length !== expected.args.length) return false;
  if (!actual.args.every((value, index) => value === expected.args[index])) return false;
  if (!actual.env || typeof actual.env !== 'object' || Array.isArray(actual.env)) return false;
  return Object.entries(expected.env).every(([key, value]) => actual.env[key] === value);
}

function claudeDesktopInfo(app, mcpUrl) {
  const p = claudeDesktopConfigPath(app);
  try {
    const { data } = readJsonDocument(p);
    const servers = mcpServersIn(data);
    const connected = !!servers
      && sameLaunch(servers[SERVER_NAME], buildMainBridgeLaunch(app, mcpUrl))
      && sameLaunch(servers[RISK_SCANNER_SERVER_NAME], buildRiskScannerLaunch(app));
    return {
      id: 'claude_desktop', name: 'Claude Desktop', method: 'config', available: true,
      connected, configPath: p,
    };
  } catch (err) {
    return {
      id: 'claude_desktop', name: 'Claude Desktop', method: 'config', available: true,
      connected: false, configPath: p, error: err.message || String(err),
    };
  }
}

function claudeDesktopConnect(app, mcpUrl) {
  const p = claudeDesktopConfigPath(app);
  const { data, raw } = readJsonDocument(p);
  data.mcpServers = mcpServersIn(data, { create: true });
  for (const k of LEGACY_SERVER_NAMES) delete data.mcpServers[k]; // 옛 이름으로 중복 등록되지 않게
  data.mcpServers[SERVER_NAME] = buildMainBridgeLaunch(app, mcpUrl);
  data.mcpServers[RISK_SCANNER_SERVER_NAME] = buildRiskScannerLaunch(app);
  writeJsonAtomic(p, data, raw);
}

function claudeDesktopDisconnect(app) {
  const p = claudeDesktopConfigPath(app);
  if (!fs.existsSync(p)) return;
  const { data, raw } = readJsonDocument(p);
  const keys = managedServerKeysIn(mcpServersIn(data));
  if (keys.length) {
    for (const k of keys) delete data.mcpServers[k]; // 옛 이름으로 남은 항목까지 정리
    writeJsonAtomic(p, data, raw);
  }
}

/** 스키마가 자주 바뀌는 클라이언트들 — 자동 쓰기 대신 복사용 스니펫만 제공. */
function manualClients(app, mcpUrl) {
  const entry = { type: 'http', url: mcpUrl };
  const scanner = buildRiskScannerLaunch(app);
  const mcpServersSnippet = JSON.stringify({
    mcpServers: { [SERVER_NAME]: entry, [RISK_SCANNER_SERVER_NAME]: scanner },
  }, null, 2);
  const serversSnippet = JSON.stringify({
    servers: {
      [SERVER_NAME]: entry,
      [RISK_SCANNER_SERVER_NAME]: { type: 'stdio', ...scanner },
    },
  }, null, 2);
  return [
    {
      id: 'cursor', name: 'Cursor', method: 'manual', available: true, connected: false,
      hint: '~/.cursor/mcp.json (또는 프로젝트 .cursor/mcp.json)',
      snippet: mcpServersSnippet,
    },
    {
      id: 'windsurf', name: 'Windsurf', method: 'manual', available: true, connected: false,
      hint: '~/.codeium/windsurf/mcp_config.json',
      snippet: mcpServersSnippet,
    },
    {
      id: 'cline', name: 'Cline', method: 'manual', available: true, connected: false,
      hint: 'VS Code Cline 확장의 MCP 설정(cline_mcp_settings.json)',
      snippet: mcpServersSnippet,
    },
    {
      id: 'vscode_copilot', name: 'VS Code Copilot', method: 'manual', available: true, connected: false,
      hint: '워크스페이스 .vscode/mcp.json (또는 명령 팔레트 "MCP: Add Server")',
      snippet: serversSnippet,
    },
  ];
}

// claude mcp get 은 실제 환경에서 수 초가 걸린다. 렌더러의 갱신 요청이 겹쳐도 같은
// CLI 를 여러 개 띄우지 않고 하나의 결과를 함께 기다리며, 잠시 재사용한다.
const DETECT_CACHE_MS = 30000;
let _detectPromise = null;
let _detectCache = null;

async function detectClients(app, mcpUrl, invoke = runClaude) {
  if (_detectCache && _detectCache.mcpUrl === mcpUrl && Date.now() < _detectCache.expiresAt) {
    return _detectCache.clients;
  }
  if (_detectPromise && _detectPromise.mcpUrl === mcpUrl) return _detectPromise.promise;

  const promise = Promise.all([
    claudeCodeInfo(app, mcpUrl, invoke),
    Promise.resolve(claudeDesktopInfo(app, mcpUrl)),
  ]).then(([cc, cd]) => {
    const clients = [cc, cd, ...manualClients(app, mcpUrl)];
    // 조회 도중 연결·해제가 있었으면 이 결과는 이미 낡았다 — 캐시에 넣지 않는다.
    if (_detectPromise?.promise === promise) {
      _detectCache = { mcpUrl, clients, expiresAt: Date.now() + DETECT_CACHE_MS };
    }
    return clients;
  }).finally(() => {
    if (_detectPromise?.promise === promise) _detectPromise = null;
  });
  _detectPromise = { mcpUrl, promise };
  return promise;
}

// 연결·해제 뒤에는 진행 중이던 조회까지 버리고 새로 본다. 실패해도 일부는 바뀌었을 수 있다.
function invalidateDetectionCache() {
  _detectCache = null;
  _detectPromise = null;
}

async function connect(app, clientId, mcpUrl, invoke = runClaude) {
  try {
    if (clientId === 'claude_code') return await claudeCodeConnect(app, mcpUrl, invoke);
    if (clientId === 'claude_desktop') return claudeDesktopConnect(app, mcpUrl);
  } finally {
    invalidateDetectionCache();
  }
  throw new Error('이 클라이언트는 자동 연결을 지원하지 않습니다 — 스니펫을 복사해 수동으로 설정하세요');
}

async function disconnect(app, clientId, invoke = runClaude) {
  try {
    if (clientId === 'claude_code') return await claudeCodeDisconnect(invoke);
    if (clientId === 'claude_desktop') return claudeDesktopDisconnect(app);
  } finally {
    invalidateDetectionCache();
  }
  throw new Error('이 클라이언트는 자동 연결 해제를 지원하지 않습니다');
}

module.exports = {
  detectClients,
  connect,
  disconnect,
  SERVER_NAME,
  RISK_SCANNER_SERVER_NAME,
  _test: {
    claudeTimeoutFor,
    parseClaudeServer,
    inspectClaudeServer,
    claudeStateMatchesPlan,
    buildRiskScannerLaunch,
    buildMainBridgeLaunch,
    buildClaudeCodeAddPlans,
    claudeCodeInfo,
    claudeCodeConnect,
    claudeCodeDisconnect,
    claudeDesktopInfo,
    claudeDesktopConnect,
    claudeDesktopDisconnect,
    manualClients,
    readJsonDocument,
    writeJsonAtomic,
    mcpServersIn,
    sameLaunch,
    resolveClaudeLauncherInPath,
    runClaude,
  },
};
