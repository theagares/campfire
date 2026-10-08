'use strict';
/**
 * main/mcp-clients.js
 * "연결" 화면에서 원클릭으로 Campfire MCP와 분리형 위험 검사기 MCP를 각 AI
 * 클라이언트에 함께 등록/해제한다.
 *
 * Claude Code / Claude Desktop 은 실제로 자동 등록까지 수행한다:
 *   - Claude Code: 공식 CLI(`claude mcp add/remove`)를 그대로 호출 — 내부 설정 파일
 *     포맷/경로를 추측하지 않고 공식 인터페이스에 위임한다(이번 세션에서 실측 검증됨).
 *   - Claude Desktop: 공식 문서화된 claude_desktop_config.json 의 mcpServers 스키마를
 *     직접 읽고 쓴다(Anthropic 공개 문서 기준 안정적인 포맷).
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
        execFile(shell, ['-lc', 'printf %s "$PATH"'], { timeout: 3000 }, (err, stdout) => {
          resolve(err ? '' : String(stdout || ''));
        });
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

async function runClaude(args) {
  const PATH = await resolveCliPath();
  const launcher = await resolveClaudeLauncher();
  return new Promise((resolve) => {
    execFile(
      launcher.command,
      [...launcher.prefixArgs, ...args],
      { timeout: 8000, windowsHide: true, env: { ...process.env, PATH } },
      (err, stdout, stderr) => {
        resolve({ ok: !err, stdout: stdout || '', stderr: stderr || '' });
      },
    );
  });
}

/** `claude mcp list` 의 `name: ...` 행을 정확히 찾는다.
 *
 * 단순 includes('campfire') 는 위험 검사기 이름(campfire-mcp-risk-scanner)만 있어도
 * 메인 MCP가 있다고 오판한다.
 */
function listedServer(stdout, name) {
  return String(stdout || '')
    .split(/\r?\n/)
    .some(line => line.trimStart().startsWith(`${name}:`));
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

async function claudeCodeInfo(invoke = runClaude) {
  const ver = await invoke(['--version']);
  if (!ver.ok) {
    return { id: 'claude_code', name: 'Claude Code', method: 'cli', available: false, connected: false };
  }
  const list = await invoke(['mcp', 'list']);
  const mainConnected = list.ok
    && [SERVER_NAME, ...LEGACY_SERVER_NAMES].some(k => listedServer(list.stdout, k));
  const scannerConnected = list.ok && listedServer(list.stdout, RISK_SCANNER_SERVER_NAME);
  const connected = mainConnected && scannerConnected;
  return { id: 'claude_code', name: 'Claude Code', method: 'cli', available: true, connected };
}

async function claudeCodeConnect(app, mcpUrl, invoke = runClaude) {
  // 기존 버전에서 campfire만 연결한 사용자는 위험 검사기만 추가해야 한다. 둘을 무조건
  // 다시 add 하면 이미 존재하는 이름 때문에 첫 명령부터 실패한다.
  const list = await invoke(['mcp', 'list']);
  if (!list.ok) throw new Error(list.stderr.trim() || 'claude mcp list 실행 실패');
  const hasMain = [SERVER_NAME, ...LEGACY_SERVER_NAMES]
    .some(name => listedServer(list.stdout, name));
  const hasScanner = listedServer(list.stdout, RISK_SCANNER_SERVER_NAME);
  const plans = buildClaudeCodeAddPlans(app, mcpUrl).filter(plan => (
    plan.name === SERVER_NAME ? !hasMain : !hasScanner
  ));
  const added = [];

  for (const plan of plans) {
    const res = await invoke(plan.args);
    if (!res.ok) {
      // add 가 설정 기록 뒤 health-check 에서 실패했을 가능성까지 포함해, 이번 연결에서
      // 새로 다룬 이름만 정리한다. 기존에 있던 사용자 설정은 건드리지 않는다.
      for (const name of [...new Set([plan.name, ...added])].reverse()) {
        await invoke(['mcp', 'remove', '--scope', 'user', name]);
      }
      throw new Error(res.stderr.trim() || `claude mcp add ${plan.name} 실행 실패`);
    }
    added.push(plan.name);
  }
}

async function claudeCodeDisconnect(invoke = runClaude) {
  // 옛 이름과 위험 검사기까지 모두 시도한다. 없는 이름을 지우면 실패하므로,
  // 하나라도 성공하면 해제된 것으로 본다(모두 없을 때만 오류).
  const results = [];
  for (const key of [SERVER_NAME, ...LEGACY_SERVER_NAMES, RISK_SCANNER_SERVER_NAME]) {
    results.push(await invoke(['mcp', 'remove', '--scope', 'user', key]));
  }
  if (!results.some(r => r.ok)) {
    throw new Error(results[0].stderr.trim() || 'claude mcp remove 실행 실패');
  }
}

function claudeDesktopConfigPath(app) {
  return path.join(app.getPath('appData'), 'Claude', 'claude_desktop_config.json');
}

function readJsonSafe(p) {
  if (!fs.existsSync(p)) return {};
  try {
    const data = JSON.parse(fs.readFileSync(p, 'utf-8'));
    return data && typeof data === 'object' && !Array.isArray(data) ? data : {};
  } catch {
    return {};
  }
}

function claudeDesktopInfo(app) {
  const p = claudeDesktopConfigPath(app);
  const data = readJsonSafe(p);
  const connected = mainServerKeysIn(data.mcpServers).length > 0
    && !!(data.mcpServers && data.mcpServers[RISK_SCANNER_SERVER_NAME]);
  return { id: 'claude_desktop', name: 'Claude Desktop', method: 'config', available: true, connected, configPath: p };
}

function claudeDesktopConnect(app, mcpUrl) {
  const p = claudeDesktopConfigPath(app);
  const data = readJsonSafe(p);
  data.mcpServers = data.mcpServers && typeof data.mcpServers === 'object' ? data.mcpServers : {};
  for (const k of LEGACY_SERVER_NAMES) delete data.mcpServers[k]; // 옛 이름으로 중복 등록되지 않게
  data.mcpServers[SERVER_NAME] = { type: 'http', url: mcpUrl };
  data.mcpServers[RISK_SCANNER_SERVER_NAME] = buildRiskScannerLaunch(app);
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, JSON.stringify(data, null, 2) + '\n', 'utf-8');
}

function claudeDesktopDisconnect(app) {
  const p = claudeDesktopConfigPath(app);
  if (!fs.existsSync(p)) return;
  const data = readJsonSafe(p);
  const keys = managedServerKeysIn(data.mcpServers);
  if (keys.length) {
    for (const k of keys) delete data.mcpServers[k]; // 옛 이름으로 남은 항목까지 정리
    fs.writeFileSync(p, JSON.stringify(data, null, 2) + '\n', 'utf-8');
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

async function detectClients(app, mcpUrl) {
  const [cc, cd] = await Promise.all([claudeCodeInfo(), Promise.resolve(claudeDesktopInfo(app))]);
  return [cc, cd, ...manualClients(app, mcpUrl)];
}

async function connect(app, clientId, mcpUrl) {
  if (clientId === 'claude_code') return claudeCodeConnect(app, mcpUrl);
  if (clientId === 'claude_desktop') return claudeDesktopConnect(app, mcpUrl);
  throw new Error('이 클라이언트는 자동 연결을 지원하지 않습니다 — 스니펫을 복사해 수동으로 설정하세요');
}

async function disconnect(app, clientId) {
  if (clientId === 'claude_code') return claudeCodeDisconnect();
  if (clientId === 'claude_desktop') return claudeDesktopDisconnect(app);
  throw new Error('이 클라이언트는 자동 연결 해제를 지원하지 않습니다');
}

module.exports = {
  detectClients,
  connect,
  disconnect,
  SERVER_NAME,
  RISK_SCANNER_SERVER_NAME,
  _test: {
    listedServer,
    buildRiskScannerLaunch,
    buildClaudeCodeAddPlans,
    claudeCodeInfo,
    claudeCodeConnect,
    claudeCodeDisconnect,
    claudeDesktopInfo,
    claudeDesktopConnect,
    claudeDesktopDisconnect,
    manualClients,
    resolveClaudeLauncherInPath,
    runClaude,
  },
};
