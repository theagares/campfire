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

const { exec } = require('child_process');
const fs = require('fs');
const path = require('path');

const SERVER_NAME = 'securedoc-gateway';

function run(cmd) {
  return new Promise((resolve) => {
    exec(cmd, { timeout: 8000, windowsHide: true }, (err, stdout, stderr) => {
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
  const connected = list.ok && list.stdout.includes(SERVER_NAME);
  return { id: 'claude_code', name: 'Claude Code', method: 'cli', available: true, connected };
}

async function claudeCodeConnect(mcpUrl) {
  const res = await run(`claude mcp add --transport http ${SERVER_NAME} ${mcpUrl} --scope user`);
  if (!res.ok) throw new Error(res.stderr.trim() || 'claude mcp add 실행 실패');
}

async function claudeCodeDisconnect() {
  const res = await run(`claude mcp remove ${SERVER_NAME} --scope user`);
  if (!res.ok) throw new Error(res.stderr.trim() || 'claude mcp remove 실행 실패');
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
  const connected = !!(data.mcpServers && data.mcpServers[SERVER_NAME]);
  return { id: 'claude_desktop', name: 'Claude Desktop', method: 'config', available: true, connected, configPath: p };
}

function claudeDesktopConnect(app, mcpUrl) {
  const p = claudeDesktopConfigPath(app);
  const data = readJsonSafe(p);
  data.mcpServers = data.mcpServers && typeof data.mcpServers === 'object' ? data.mcpServers : {};
  data.mcpServers[SERVER_NAME] = { type: 'http', url: mcpUrl };
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, JSON.stringify(data, null, 2) + '\n', 'utf-8');
}

function claudeDesktopDisconnect(app) {
  const p = claudeDesktopConfigPath(app);
  if (!fs.existsSync(p)) return;
  const data = readJsonSafe(p);
  if (data.mcpServers && data.mcpServers[SERVER_NAME]) {
    delete data.mcpServers[SERVER_NAME];
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
