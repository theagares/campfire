'use strict';
/**
 * 설치본에 번들된 Python으로 Campfire stdio shim을 직접 띄우고, 실행 중인 HTTP MCP까지
 * initialize -> tools/list가 왕복하는지 확인한다.
 *
 * 사용:
 *   node scripts/smoke-mcp-stdio.js \
 *     --python <번들 Python> --engine-dir <번들 engine> --url http://127.0.0.1:48200/mcp
 */

const { spawn } = require('child_process');
const os = require('os');
const path = require('path');
const readline = require('readline');

function option(name) {
  const index = process.argv.indexOf(`--${name}`);
  return index >= 0 ? process.argv[index + 1] : '';
}

function requiredOption(name) {
  const value = option(name);
  if (!value) throw new Error(`--${name} 값이 필요합니다`);
  return value;
}

async function main() {
  const python = requiredOption('python');
  const engineDir = requiredOption('engine-dir');
  const url = requiredOption('url');
  const child = spawn(python, ['-m', 'app.adapters.mcp.stdio_shim'], {
    cwd: engineDir,
    windowsHide: true,
    stdio: ['pipe', 'pipe', 'pipe'],
    env: {
      ...process.env,
      PYTHONPATH: engineDir,
      PYTHONUTF8: '1',
      PYTHONPYCACHEPREFIX: path.join(os.tmpdir(), 'campfire-mcp-smoke-pycache'),
      SECUREDOC_MCP_URL: url,
    },
  });

  let nextId = 1;
  let stderr = '';
  let exited = false;
  const pending = new Map();
  const rejectPending = (error) => {
    for (const request of pending.values()) {
      clearTimeout(request.timer);
      request.reject(error);
    }
    pending.clear();
  };

  child.stderr.setEncoding('utf8');
  child.stderr.on('data', chunk => { stderr += chunk; });
  child.on('error', rejectPending);
  child.on('exit', (code, signal) => {
    exited = true;
    if (pending.size) {
      const detail = stderr.trim();
      rejectPending(new Error(
        `stdio shim이 응답 전에 종료됐습니다(code=${code}, signal=${signal})${detail ? `: ${detail}` : ''}`,
      ));
    }
  });

  const lines = readline.createInterface({ input: child.stdout, crlfDelay: Infinity });
  lines.on('line', (line) => {
    if (!line.trim()) return;
    let message;
    try {
      message = JSON.parse(line);
    } catch (error) {
      rejectPending(new Error(`stdio shim이 JSON이 아닌 stdout을 출력했습니다: ${line}`));
      return;
    }
    if (message.id === undefined || message.id === null) return;
    const request = pending.get(message.id);
    if (!request) return;
    pending.delete(message.id);
    clearTimeout(request.timer);
    if (message.error) request.reject(new Error(JSON.stringify(message.error)));
    else request.resolve(message.result);
  });

  const send = (message) => {
    child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', ...message })}\n`);
  };
  const request = (method, params = {}) => new Promise((resolve, reject) => {
    const id = nextId;
    nextId += 1;
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error(`${method} 응답을 30초 안에 받지 못했습니다${stderr.trim() ? `: ${stderr.trim()}` : ''}`));
    }, 30000);
    pending.set(id, { resolve, reject, timer });
    send({ id, method, params });
  });

  try {
    await request('initialize', {
      protocolVersion: '2025-06-18',
      capabilities: {},
      clientInfo: { name: 'campfire-packaged-smoke', version: '1.0.0' },
    });
    send({ method: 'notifications/initialized', params: {} });
    const result = await request('tools/list');
    const names = new Set((result.tools || []).map(tool => tool.name));
    for (const expected of ['scan_text', 'scan_file', 'get_status']) {
      if (!names.has(expected)) throw new Error(`MCP 도구가 없습니다: ${expected}`);
    }
    console.log(`[mcp-smoke] stdio -> HTTP MCP 정상 (${[...names].sort().join(', ')})`);
  } finally {
    lines.close();
    child.stdin.end();
    if (!exited) {
      await new Promise(resolve => {
        const timer = setTimeout(() => {
          child.kill();
          resolve();
        }, 3000);
        child.once('exit', () => {
          clearTimeout(timer);
          resolve();
        });
      });
    }
  }
}

main().catch((error) => {
  console.error(`[mcp-smoke] 실패: ${error.message}`);
  process.exitCode = 1;
});
