'use strict';
/**
 * preload 가 렌더러에 노출한 invoke 채널마다 메인(ipc.js)에 핸들러가 있어야 한다.
 * v0.2.26 에서 tray-preload 를 보지 않고 "죽은 핸들러"로 app:quit·window:showDashboard 를 지워,
 * 트레이 팝오버의 QUIT·대시보드 버튼이 무반응이었다(호출하면 "No handler registered").
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const read = (f) => fs.readFileSync(path.join(__dirname, '..', 'main', f), 'utf8');

test('preload·tray-preload 의 invoke 채널은 모두 ipc.js 에 핸들러가 있다', () => {
  const handled = new Set([...read('ipc.js').matchAll(/ipcMain\.handle\('([^']+)'/g)].map((m) => m[1]));
  for (const f of ['preload.js', 'tray-preload.js']) {
    const invoked = [...read(f).matchAll(/ipcRenderer\.invoke\('([^']+)'/g)].map((m) => m[1]);
    assert.ok(invoked.length > 0, `${f} 에서 채널을 못 읽었다`);
    const missing = invoked.filter((ch) => !handled.has(ch));
    assert.deepEqual(missing, [], `${f} 가 부르는데 핸들러가 없는 채널`);
  }
});
