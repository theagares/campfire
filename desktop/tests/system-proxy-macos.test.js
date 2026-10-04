'use strict';

const test = require('node:test');
const assert = require('node:assert');

const mac = require('../main/system-proxy-macos');
const { create } = require('../main/system-proxy');

test('networksetup 출력에서 비활성 서비스와 프록시 상태를 구분한다', () => {
  assert.deepEqual(mac.parseServices([
    'An asterisk (*) denotes that a network service is disabled.',
    'Wi-Fi',
    '*Thunderbolt Bridge',
    'USB 10/100/1000 LAN',
    '',
  ].join('\n')), [
    { name: 'Wi-Fi', disabled: false },
    { name: 'Thunderbolt Bridge', disabled: true },
    { name: 'USB 10/100/1000 LAN', disabled: false },
  ]);
  assert.deepEqual(mac.parseAutoProxy('URL: (null)\nEnabled: No\n'), {
    pacEnabled: false, pacUrl: null,
  });
  assert.deepEqual(mac.parseManualProxy('Enabled: Yes\nServer: corp.example\nPort: 8080\n'), {
    enabled: true, server: 'corp.example', port: 8080,
  });
  assert.equal(mac.discoveryEnabled('Auto Proxy Discovery: On\n'), true);
  assert.equal(mac.discoveryEnabled('Auto Proxy Discovery: Off\n'), false);
});

test('macOS 서비스 전체가 우리 PAC일 때만 적용 상태다', () => {
  const prefix = 'http://127.0.0.1:48211/campfire.pac';
  const sp = create(async () => {}, 'darwin');
  const clean = {
    platform: 'darwin',
    services: [
      { name: 'Wi-Fi', pacEnabled: true, pacUrl: `${prefix}?v=1`, web: {}, secureWeb: {} },
      { name: 'Ethernet', pacEnabled: true, pacUrl: `${prefix}?v=1`, web: {}, secureWeb: {} },
    ],
  };
  assert.equal(sp.isOurs(clean, prefix), true);
  assert.equal(sp.conflict(clean, prefix), null);

  const partial = structuredClone(clean);
  partial.services[1].pacEnabled = false;
  assert.equal(sp.isOurs(partial, prefix), true, '일부라도 남았으면 워치독 복원 대상이다');
  assert.equal(sp.isFullyOurs(partial, prefix), false, 'UI에는 전체 적용으로 표시하면 안 된다');

  const corporate = structuredClone(clean);
  corporate.services[0].pacUrl = 'https://corp.example/proxy.pac';
  assert.match(sp.conflict(corporate, prefix), /corp\.example.*Wi-Fi/);

  const manual = structuredClone(clean);
  manual.services[0].web = { enabled: true, server: 'proxy.corp', port: 3128 };
  assert.match(sp.conflict(manual, prefix), /proxy\.corp:3128.*Wi-Fi/);

  const socks = structuredClone(clean);
  socks.services[0].socks = { enabled: true, server: 'socks.corp', port: 1080 };
  assert.match(sp.conflict(socks, prefix), /socks\.corp:1080.*Wi-Fi/);
});

test('macOS runner는 모든 활성 서비스에 PAC를 적용하고 원래 값으로 복원한다', async () => {
  const calls = [];
  let state = { pacEnabled: false, pacUrl: null };
  let privilegedCount = 0;
  function fakeExec(file, args, _options, callback) {
    calls.push([file, ...args]);
    if (file === '/usr/bin/osascript') {
      // 실제 networksetup 은 빈 URL 을 거부한다 — mock 도 같게 굴어야 그 회귀를 잡는다.
      if (/-setautoproxyurl' '[^']*' ''/.test(args.at(-1))) {
        callback(new Error('exit 4'), '', "execution error: ** Error: The parameters were not valid. (4)");
        return;
      }
      privilegedCount += 1;
      state = privilegedCount === 1
        ? { pacEnabled: true, pacUrl: 'http://127.0.0.1:48211/campfire.pac?v=1' }
        : { pacEnabled: false, pacUrl: null };
      callback(null, '', '');
      return;
    }
    if (args[0] === '-listallnetworkservices') {
      callback(null, 'An asterisk (*) denotes that a network service is disabled.\nWi-Fi\n*Bridge\n', '');
    } else if (args[0] === '-getautoproxyurl') {
      callback(null, `URL: ${state.pacUrl || '(null)'}\nEnabled: ${state.pacEnabled ? 'Yes' : 'No'}\n`, '');
    } else if (args[0] === '-getproxyautodiscovery') {
      callback(null, 'Auto Proxy Discovery: Off\n', '');
    } else if (args[0] === '-getwebproxy' || args[0] === '-getsecurewebproxy'
        || args[0] === '-getsocksfirewallproxy') {
      callback(null, 'Enabled: No\nServer: (null)\nPort: 0\nAuthenticated Proxy Enabled: 0\n', '');
    } else {
      callback(new Error(`unexpected ${args.join(' ')}`), '', '');
    }
  }

  const run = mac.createRunner({ execFileImpl: fakeExec });
  const previous = await run('query');
  const applied = await run('set-pac', 'http://127.0.0.1:48211/campfire.pac?v=1');
  assert.equal(applied.services[0].pacEnabled, true);
  assert.equal(applied.services[0].pacUrl, 'http://127.0.0.1:48211/campfire.pac?v=1');
  await run('restore', JSON.stringify(previous));

  const elevated = calls.filter((c) => c[0] === '/usr/bin/osascript');
  assert.equal(elevated.length, 2);
  assert.match(elevated[0].at(-1), /-setautoproxyurl.*Wi-Fi.*campfire\.pac/);
  assert.match(elevated[0].at(-1), /-setautoproxystate.*Wi-Fi.*on/);
  assert.match(elevated[0].at(-1), /-setproxyautodiscovery.*Wi-Fi.*off/);
  assert.match(elevated[1].at(-1), /-setautoproxystate.*Wi-Fi.*off/);
  assert.ok(!/-setautoproxyurl/.test(elevated[1].at(-1)), '원래 PAC 가 없었으면 URL 을 쓰지 않는다(빈 URL 은 거부된다)');
  assert.ok(!/set -e/.test(elevated[1].at(-1)), '복원은 첫 실패에서 멈추지 않고 모든 서비스를 되돌린다');
  assert.ok(!elevated.some((c) => c.at(-1).includes('Bridge')), '비활성 서비스는 바꾸면 안 된다');
});

test('셸 인자는 서비스 이름의 메타문자를 데이터로만 취급한다', () => {
  assert.equal(mac.shellQuote("Wi-Fi'; touch /tmp/pwn; '"), "'Wi-Fi'\\''; touch /tmp/pwn; '\\'''" );
});
