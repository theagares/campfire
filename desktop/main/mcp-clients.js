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

/**
 * 설정 파일을 읽어 { status, data } 로 돌려준다.
 *
 * "없다" 와 "있는데 못 읽겠다" 를 반드시 구분해야 한다. 예전엔 둘 다 {} 로 뭉갰는데,
 * 그러면 파싱에 실패한 순간 아래 connect 가 그 {} 위에 우리 항목 하나만 얹어 파일을
 * 통째로 다시 쓴다 — **사용자가 등록해둔 다른 MCP 서버와 설정이 전부 사라진다.**
 * 트레일링 콤마나 주석 하나, 편집 중 저장 같은 흔한 상황으로도 걸린다.
 *
 * 이 파일은 우리 것이 아니다. 이 모듈이 Cursor/Windsurf 를 자동 쓰기 대상에서 뺀 이유
 * (상단 주석: "잘못 자동으로 써버리면 사용자의 기존 설정을 조용히 깨뜨릴 위험")가
 * 여기에도 똑같이 적용된다 — 스키마가 안정적인 것과 파싱이 항상 성공하는 것은 다른
 * 얘기다. 못 읽으면 손대지 않고 사용자에게 알린다.
 *
 * status: 'missing'    파일 없음 → 새로 만들어도 안전
 *         'ok'         읽었다 → data 사용
 *         'unreadable' 있는데 JSON 이 아니거나 객체가 아님 → **쓰지 않는다**
 */
function readConfig(p) {
  if (!fs.existsSync(p)) return { status: 'missing', data: {} };
  let raw;
  try {
    raw = fs.readFileSync(p, 'utf-8');
  } catch (err) {
    return { status: 'unreadable', data: {}, reason: err.message };
  }
  if (raw.trim() === '') return { status: 'missing', data: {} }; // 빈 파일은 없는 것과 같다
  try {
    const data = JSON.parse(raw);
    if (!data || typeof data !== 'object' || Array.isArray(data)) {
      return { status: 'unreadable', data: {}, reason: '최상위가 JSON 객체가 아닙니다' };
    }
    return { status: 'ok', data };
  } catch (err) {
    return { status: 'unreadable', data: {}, reason: err.message };
  }
}

function unreadableError(p, reason) {
  return new Error(
    `Claude Desktop 설정 파일을 읽을 수 없어 수정하지 않았습니다 (${reason}).\n` +
    `${p} 를 확인해 JSON 문법을 고친 뒤 다시 시도하세요. ` +
    '덮어쓰면 기존 설정이 사라지므로 그대로 두었습니다.'
  );
}

/** 원자적 쓰기 + 직전 상태 백업.
 *
 *  writeFileSync 는 truncate 후 쓰기라, 중간에 실패하면 설정이 깨진 채 남는다.
 *  임시 파일에 다 쓴 뒤 rename 하면 파일이 항상 "이전 내용" 아니면 "새 내용" 이다
 *  (rename 은 같은 디렉터리 안에서 원자적이고, Windows 에서도 기존 파일을 교체한다).
 *  그리고 우리가 뭔가 잘못했을 때 되돌릴 수 있게, 바꾸기 직전 내용을 .bak 로 남긴다. */
function writeConfigAtomic(p, data) {
  const dir = path.dirname(p);
  fs.mkdirSync(dir, { recursive: true });
  if (fs.existsSync(p)) {
    try {
      fs.copyFileSync(p, `${p}.campfire-backup`);
    } catch (err) {
      console.error('[mcp-clients] 설정 백업 실패(계속 진행):', err.message);
    }
  }
  const tmp = path.join(dir, `.${path.basename(p)}.campfire-tmp`);
  fs.writeFileSync(tmp, JSON.stringify(data, null, 2) + '\n', 'utf-8');
  fs.renameSync(tmp, p);
}

function claudeDesktopInfo(app) {
  const p = claudeDesktopConfigPath(app);
  const cfg = readConfig(p);
  const connected = cfg.status === 'ok' && serverKeysIn(cfg.data.mcpServers).length > 0;
  return {
    id: 'claude_desktop',
    name: 'Claude Desktop',
    method: 'config',
    available: true,
    connected,
    configPath: p,
    // 화면이 "왜 연결 버튼이 실패하는지" 를 미리 보여줄 수 있게 알려준다.
    configUnreadable: cfg.status === 'unreadable',
    configIssue: cfg.status === 'unreadable' ? cfg.reason : null,
  };
}

function claudeDesktopConnect(app, mcpUrl) {
  const p = claudeDesktopConfigPath(app);
  const cfg = readConfig(p);
  if (cfg.status === 'unreadable') throw unreadableError(p, cfg.reason);

  const data = cfg.data;
  data.mcpServers = data.mcpServers && typeof data.mcpServers === 'object' && !Array.isArray(data.mcpServers)
    ? data.mcpServers
    : {};
  for (const k of LEGACY_SERVER_NAMES) delete data.mcpServers[k]; // 옛 이름으로 중복 등록되지 않게
  data.mcpServers[SERVER_NAME] = { type: 'http', url: mcpUrl };
  writeConfigAtomic(p, data);
}

function claudeDesktopDisconnect(app) {
  const p = claudeDesktopConfigPath(app);
  const cfg = readConfig(p);
  if (cfg.status === 'missing') return;
  // 해제하려다 전체를 잃는 게 제일 나쁘다 — 못 읽으면 그대로 둔다.
  if (cfg.status === 'unreadable') throw unreadableError(p, cfg.reason);

  const keys = serverKeysIn(cfg.data.mcpServers);
  if (keys.length) {
    for (const k of keys) delete cfg.data.mcpServers[k]; // 옛 이름으로 남은 항목까지 정리
    writeConfigAtomic(p, cfg.data);
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
