'use strict';
/**
 * mcp-clients-config.test.js
 *
 * Claude Desktop 의 claude_desktop_config.json 은 **우리 파일이 아니다.** 거기에
 * 우리 항목 하나를 넣고 빼는 게 전부인데, 예전 구현은 파싱에 실패하면 그 파일을
 * 빈 객체로 간주하고 통째로 다시 썼다 — 트레일링 콤마 하나로 사용자가 등록해둔
 * 다른 MCP 서버와 설정이 전부 사라진다.
 *
 * 이 테스트가 지키는 것:
 *   1. 못 읽는 파일은 건드리지 않는다(내용이 1바이트도 안 바뀐다).
 *   2. 읽을 수 있으면 다른 키를 보존한 채 우리 항목만 더한다/뺀다.
 *   3. 파일이 없으면 새로 만든다(과잉 방어로 정상 흐름을 막지 않는다).
 *   4. 수정 시 직전 내용을 .campfire-backup 으로 남긴다.
 *
 * 실행: node --test tests/mcp-clients-config.test.js
 */

const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const mcpClients = require('../main/mcp-clients');

const MCP_URL = 'http://127.0.0.1:48200/mcp';

/** app.getPath('appData') 만 쓰므로 그것만 흉내낸다. */
function makeApp(appDataDir) {
  return { getPath: (k) => (k === 'appData' ? appDataDir : appDataDir) };
}

function setup(configText) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'campfire-mcp-'));
  const configPath = path.join(dir, 'Claude', 'claude_desktop_config.json');
  if (configText !== null) {
    fs.mkdirSync(path.dirname(configPath), { recursive: true });
    fs.writeFileSync(configPath, configText, 'utf-8');
  }
  return { app: makeApp(dir), configPath, dir };
}

// ── 1. 못 읽는 파일은 건드리지 않는다 ────────────────────────────────────────
const BROKEN = [
  ['트레일링 콤마', '{\n  "mcpServers": {\n    "other": { "url": "x" },\n  }\n}\n'],
  ['잘린 파일(편집 중 저장)', '{\n  "mcpServers": {\n    "other": {'],
  ['주석이 들어간 JSON', '{\n  // 내 서버\n  "mcpServers": { "other": { "url": "x" } }\n}\n'],
  ['최상위가 배열', '[{"mcpServers": {}}]\n'],
];

for (const [label, text] of BROKEN) {
  test(`연결: 못 읽는 설정(${label})은 덮어쓰지 않고 실패한다`, async () => {
    const { app, configPath } = setup(text);
    const before = fs.readFileSync(configPath, 'utf-8');

    // connect/disconnect 는 async 다 — assert.throws 로는 거부를 못 잡는다.
    await assert.rejects(
      () => mcpClients.connect(app, 'claude_desktop', MCP_URL),
      /읽을 수 없어 수정하지 않았습니다/,
    );

    assert.equal(fs.readFileSync(configPath, 'utf-8'), before,
      '못 읽는 설정 파일이 수정됐다 — 사용자 설정이 날아간다');
  });

  test(`해제: 못 읽는 설정(${label})도 건드리지 않는다`, async () => {
    const { app, configPath } = setup(text);
    const before = fs.readFileSync(configPath, 'utf-8');

    await assert.rejects(() => mcpClients.disconnect(app, 'claude_desktop'));
    assert.equal(fs.readFileSync(configPath, 'utf-8'), before,
      '해제하려다 설정 파일 전체를 잃었다');
  });
}

test('detect: 못 읽는 설정은 connected=false 로 두되 이유를 알려준다', async () => {
  const { app } = setup('{ 깨진 }');
  const clients = await mcpClients.detectClients(app, MCP_URL);
  const cd = clients.find((c) => c.id === 'claude_desktop');
  assert.equal(cd.connected, false);
  assert.equal(cd.configUnreadable, true);
  assert.ok(cd.configIssue, '왜 못 읽는지 알려줘야 사용자가 고칠 수 있다');
});

// ── 2. 정상 파일: 다른 키를 보존한다 ─────────────────────────────────────────
test('연결: 기존 서버와 무관한 키를 그대로 둔다', async () => {
  const original = {
    theme: 'dark',
    mcpServers: {
      'someone-elses': { command: 'node', args: ['server.js'] },
    },
  };
  const { app, configPath } = setup(JSON.stringify(original, null, 2));

  await mcpClients.connect(app, 'claude_desktop', MCP_URL);

  const after = JSON.parse(fs.readFileSync(configPath, 'utf-8'));
  assert.equal(after.theme, 'dark', '우리와 무관한 최상위 키가 사라졌다');
  assert.deepEqual(after.mcpServers['someone-elses'], original.mcpServers['someone-elses'],
    '다른 MCP 서버 등록이 사라졌다');
  assert.deepEqual(after.mcpServers[mcpClients.SERVER_NAME], { type: 'http', url: MCP_URL });
});

test('해제: 우리 항목만 빼고 나머지는 남긴다', async () => {
  const { app, configPath } = setup(JSON.stringify({
    theme: 'dark',
    mcpServers: {
      'someone-elses': { command: 'node' },
      [mcpClients.SERVER_NAME]: { type: 'http', url: MCP_URL },
      'securedoc-gateway': { type: 'http', url: 'http://old' },  // 리브랜딩 이전 키
    },
  }, null, 2));

  await mcpClients.disconnect(app, 'claude_desktop');

  const after = JSON.parse(fs.readFileSync(configPath, 'utf-8'));
  assert.equal(after.theme, 'dark');
  assert.ok(after.mcpServers['someone-elses']);
  assert.equal(after.mcpServers[mcpClients.SERVER_NAME], undefined);
  assert.equal(after.mcpServers['securedoc-gateway'], undefined, '옛 키도 정리돼야 한다');
});

// ── 3. 없으면 만든다 (과잉 방어로 정상 흐름을 막지 않는다) ────────────────────
test('연결: 설정 파일이 없으면 새로 만든다', async () => {
  const { app, configPath } = setup(null);
  await mcpClients.connect(app, 'claude_desktop', MCP_URL);
  const after = JSON.parse(fs.readFileSync(configPath, 'utf-8'));
  assert.deepEqual(after.mcpServers[mcpClients.SERVER_NAME], { type: 'http', url: MCP_URL });
});

test('연결: 빈 파일은 "없는 것"으로 보고 정상 생성한다', async () => {
  const { app, configPath } = setup('   \n');
  await mcpClients.connect(app, 'claude_desktop', MCP_URL);
  const after = JSON.parse(fs.readFileSync(configPath, 'utf-8'));
  assert.ok(after.mcpServers[mcpClients.SERVER_NAME]);
});

test('해제: 설정 파일이 없으면 조용히 넘어간다', async () => {
  const { app } = setup(null);
  await mcpClients.disconnect(app, 'claude_desktop');  // 거부하면 테스트가 실패한다
});

// ── 4. 백업 / 원자적 쓰기 ────────────────────────────────────────────────────
test('수정 직전 내용을 .campfire-backup 으로 남긴다', async () => {
  const original = JSON.stringify({ mcpServers: { keep: { url: 'x' } } }, null, 2);
  const { app, configPath } = setup(original);

  await mcpClients.connect(app, 'claude_desktop', MCP_URL);

  const backup = `${configPath}.campfire-backup`;
  assert.ok(fs.existsSync(backup), '백업이 없다 — 우리가 잘못 써도 되돌릴 수 없다');
  assert.equal(fs.readFileSync(backup, 'utf-8'), original);
});

test('임시 파일을 남기지 않는다', async () => {
  const { app, configPath } = setup('{}');
  await mcpClients.connect(app, 'claude_desktop', MCP_URL);
  const leftovers = fs.readdirSync(path.dirname(configPath)).filter((f) => f.includes('campfire-tmp'));
  assert.deepEqual(leftovers, [], `임시 파일이 남았다: ${leftovers}`);
});
