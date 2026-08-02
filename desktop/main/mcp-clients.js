'use strict';
/**
 * main/mcp-clients.js
 * "연결" 화면에서 원클릭으로 로컬 MCP 서버(/mcp)를 각 AI 클라이언트에 등록/해제한다.
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

const { exec, execFile } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const SERVER_NAME = 'campfire';
// 리브랜딩 이전에 등록해둔 키. 사용자의 claude_desktop_config.json 에 그대로 남아 있어서,
// 새 키만 보면 "연결 안 됨" 으로 보이고 해제해도 옛 항목이 계속 남는다. 조회·해제 때
// 둘 다 취급하고, 연결할 때는 옛 항목을 지우고 새 키로 바꿔 쓴다.
const LEGACY_SERVER_NAMES = ['securedoc-gateway'];
const serverKeysIn = (servers) =>
  [SERVER_NAME, ...LEGACY_SERVER_NAMES].filter(k => servers && servers[k]);

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

async function run(cmd) {
  const PATH = await resolveCliPath();
  return new Promise((resolve) => {
    exec(cmd, { timeout: 8000, windowsHide: true, env: { ...process.env, PATH } }, (err, stdout, stderr) => {
      resolve({ ok: !err, stdout: stdout || '', stderr: stderr || '' });
    });
  });
}

async function claudeCodeInfo() {
  const ver = await run('claude --version');
  if (!ver.ok) {
    return { id: 'claude_code', name: 'Claude Code', method: 'cli', available: false, connected: false };
  }
  const list = await run('claude mcp list');
  const connected = list.ok
    && [SERVER_NAME, ...LEGACY_SERVER_NAMES].some(k => list.stdout.includes(k));
  return { id: 'claude_code', name: 'Claude Code', method: 'cli', available: true, connected };
}

async function claudeCodeConnect(mcpUrl) {
  const res = await run(`claude mcp add --transport http ${SERVER_NAME} ${mcpUrl} --scope user`);
  if (!res.ok) throw new Error(res.stderr.trim() || 'claude mcp add 실행 실패');
}

async function claudeCodeDisconnect() {
  // 옛 이름으로 등록돼 있을 수 있어 둘 다 시도한다. 없는 이름을 지우면 실패하므로,
  // 하나라도 성공하면 해제된 것으로 본다(둘 다 없을 때만 오류).
  const results = [];
  for (const key of [SERVER_NAME, ...LEGACY_SERVER_NAMES]) {
    results.push(await run(`claude mcp remove ${key} --scope user`));
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
  const connected = serverKeysIn(data.mcpServers).length > 0;
  return { id: 'claude_desktop', name: 'Claude Desktop', method: 'config', available: true, connected, configPath: p };
}

function claudeDesktopConnect(app, mcpUrl) {
  const p = claudeDesktopConfigPath(app);
  const data = readJsonSafe(p);
  data.mcpServers = data.mcpServers && typeof data.mcpServers === 'object' ? data.mcpServers : {};
  for (const k of LEGACY_SERVER_NAMES) delete data.mcpServers[k]; // 옛 이름으로 중복 등록되지 않게
  data.mcpServers[SERVER_NAME] = { type: 'http', url: mcpUrl };
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, JSON.stringify(data, null, 2) + '\n', 'utf-8');
}

function claudeDesktopDisconnect(app) {
  const p = claudeDesktopConfigPath(app);
  if (!fs.existsSync(p)) return;
  const data = readJsonSafe(p);
  const keys = serverKeysIn(data.mcpServers);
  if (keys.length) {
    for (const k of keys) delete data.mcpServers[k]; // 옛 이름으로 남은 항목까지 정리
    fs.writeFileSync(p, JSON.stringify(data, null, 2) + '\n', 'utf-8');
  }
}

/** 스키마가 자주 바뀌는 클라이언트들 — 자동 쓰기 대신 복사용 스니펫만 제공. */
function manualClients(mcpUrl) {
  const entry = { type: 'http', url: mcpUrl };
  const mcpServersSnippet = JSON.stringify({ mcpServers: { [SERVER_NAME]: entry } }, null, 2);
  const serversSnippet = JSON.stringify({ servers: { [SERVER_NAME]: entry } }, null, 2);
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
  return [cc, cd, ...manualClients(mcpUrl)];
}

async function connect(app, clientId, mcpUrl) {
  if (clientId === 'claude_code') return claudeCodeConnect(mcpUrl);
  if (clientId === 'claude_desktop') return claudeDesktopConnect(app, mcpUrl);
  throw new Error('이 클라이언트는 자동 연결을 지원하지 않습니다 — 스니펫을 복사해 수동으로 설정하세요');
}

async function disconnect(app, clientId) {
  if (clientId === 'claude_code') return claudeCodeDisconnect();
  if (clientId === 'claude_desktop') return claudeDesktopDisconnect(app);
  throw new Error('이 클라이언트는 자동 연결 해제를 지원하지 않습니다');
}

module.exports = { detectClients, connect, disconnect, SERVER_NAME };
