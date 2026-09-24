'use strict';
/**
 * system-proxy 의 **진짜** PowerShell 경로를 탄다 — 단, 설정은 안 바꾼다.
 *
 * proxy-toggle.test.js 는 runner 를 가짜로 갈아끼워 로직만 본다. 그래서 이 버그를
 * 못 잡았다: powershell.exe 가 -File 인자의 큰따옴표를 지워 restore 의 JSON 이
 * {flags:9,...} 로 도착했고, 해석에서 매번 죽었다. 토글을 꺼도 PAC 가 안 풀리는
 * 버그였는데 가짜 runner 로는 절대 안 보인다.
 *
 * 여기서는 echo(받은 값을 그대로 돌려줌)와 query(읽기)만 부른다. 둘 다 사용자
 * 설정을 건드리지 않는다. Windows 가 아니면 건너뛴다.
 */

const test = require('node:test');
const assert = require('node:assert');

const { runScript } = require('../main/system-proxy');

const onWindows = process.platform === 'win32';

test('인자가 한 글자도 안 바뀌고 도착한다', { skip: !onWindows }, async () => {
  const payloads = [
    // restore 가 실제로 받는 모양 — 이게 {flags:9,...} 로 깨졌었다.
    JSON.stringify({ flags: 9, server: null, bypass: '127.0.0.1:16107', pacUrl: null }),
    'http://127.0.0.1:48211/campfire.pac?v=abc123&x=1',
    'say "hi" to \\server\\share with spaces',
    '회사 프록시 <local>; 설정',
  ];
  for (const p of payloads) {
    const { echo } = await runScript('echo', p);
    assert.strictEqual(echo, p, `깨져서 도착했다: ${echo}`);
  }
});

test('query 는 읽기만 한다 — 모양 확인', { skip: !onWindows }, async () => {
  const s = await runScript('query');
  assert.strictEqual(typeof s.flags, 'number');
  assert.ok('server' in s && 'bypass' in s && 'pacUrl' in s);
});
