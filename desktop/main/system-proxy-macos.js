'use strict';
/**
 * macOS 시스템 프록시를 PAC 로 바꾸고 원래 상태로 되돌린다.
 *
 * macOS 프록시는 네트워크 서비스(Wi-Fi, Ethernet 등)마다 따로 저장된다. 현재
 * 활성 서비스만 바꾸면 사용 중 Wi-Fi/Ethernet 전환 시 검사가 우회될 수 있으므로,
 * 비활성화되지 않은 서비스를 모두 같은 PAC 로 설정하고 서비스별 원래 값을 보관한다.
 * 쓰기에는 관리자 승인이 필요해서 한 번의 osascript 인증 창으로 묶는다.
 */

const { execFile } = require('child_process');

const NETWORK_SETUP = '/usr/sbin/networksetup';
const OSASCRIPT = '/usr/bin/osascript';

function execFileAsync(execFileImpl, file, args, options = {}) {
  return new Promise((resolve, reject) => {
    execFileImpl(file, args, { timeout: 30000, ...options }, (err, stdout = '', stderr = '') => {
      if (err) {
        const detail = String(stderr || err.message || '').trim();
        reject(new Error(`${file} 실패: ${detail}`));
        return;
      }
      resolve(String(stdout));
    });
  });
}

function parseServices(output) {
  return String(output).split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line && !/denotes that a network service is disabled/i.test(line))
    .map((line) => ({ name: line.replace(/^\*\s*/, ''), disabled: line.startsWith('*') }));
}

function field(output, name) {
  const m = String(output).match(new RegExp(`^${name}:\\s*(.*)$`, 'im'));
  return m ? m[1].trim() : '';
}

function enabled(output) {
  return /^(yes|on|1|true)$/i.test(field(output, 'Enabled'));
}

function discoveryEnabled(output) {
  return /:\s*(yes|on|1|true)\s*$/im.test(String(output));
}

function nullable(value) {
  return !value || /^\(null\)$/i.test(value) ? null : value;
}

function parseAutoProxy(output) {
  return {
    pacEnabled: enabled(output),
    pacUrl: nullable(field(output, 'URL')),
  };
}

function parseManualProxy(output) {
  const port = Number.parseInt(field(output, 'Port'), 10);
  return {
    enabled: enabled(output),
    server: nullable(field(output, 'Server')),
    port: Number.isFinite(port) ? port : null,
  };
}

// 셸을 거치지만 모든 동적 값은 단일 인자로 인용한다. 서비스 이름에 따옴표나
// 셸 메타문자가 있어도 명령으로 해석되지 않는다.
function shellQuote(value) {
  return `'${String(value).replace(/'/g, `'\\''`)}'`;
}

function networkSetupCommand(args) {
  return [NETWORK_SETUP, ...args].map(shellQuote).join(' ');
}

function createRunner({ execFileImpl = execFile } = {}) {
  const run = (file, args, options) => execFileAsync(execFileImpl, file, args, options);

  async function listEnabledServices() {
    const output = await run(NETWORK_SETUP, ['-listallnetworkservices']);
    return parseServices(output).filter((service) => !service.disabled);
  }

  async function query() {
    const services = await listEnabledServices();
    const states = await Promise.all(services.map(async ({ name }) => {
      const [auto, discovery, web, secureWeb, socks] = await Promise.all([
        run(NETWORK_SETUP, ['-getautoproxyurl', name]),
        run(NETWORK_SETUP, ['-getproxyautodiscovery', name]),
        run(NETWORK_SETUP, ['-getwebproxy', name]),
        run(NETWORK_SETUP, ['-getsecurewebproxy', name]),
        run(NETWORK_SETUP, ['-getsocksfirewallproxy', name]),
      ]);
      return {
        name,
        ...parseAutoProxy(auto),
        autoDiscoveryEnabled: discoveryEnabled(discovery),
        web: parseManualProxy(web),
        secureWeb: parseManualProxy(secureWeb),
        socks: parseManualProxy(socks),
      };
    }));
    return { platform: 'darwin', services: states };
  }

  async function runPrivileged(calls) {
    if (!calls.length) throw new Error('설정할 macOS 네트워크 서비스가 없습니다');
    const command = ['set -e', ...calls.map(networkSetupCommand)].join('; ');
    // argv 로 넘겨 AppleScript 문자열에 값을 삽입하지 않는다. macOS가 관리자 암호나
    // Touch ID 창을 직접 띄우며, 취소하면 이 Promise가 실패한다.
    await run(OSASCRIPT, [
      '-e', 'on run argv',
      '-e', 'do shell script (item 1 of argv) with administrator privileges',
      '-e', 'end run',
      command,
    ], { timeout: 120000 });
  }

  async function setPac(url) {
    if (!/^http:\/\/127\.0\.0\.1(?::\d+)?\//.test(String(url))) {
      throw new Error('로컬 PAC URL만 설정할 수 있습니다');
    }
    const before = await query();
    const calls = [];
    for (const service of before.services) {
      calls.push(['-setproxyautodiscovery', service.name, 'off']);
      calls.push(['-setautoproxyurl', service.name, String(url)]);
      calls.push(['-setautoproxystate', service.name, 'on']);
    }
    await runPrivileged(calls);
    const after = await query();
    if (!after.services.length
        || after.services.some((s) => !s.pacEnabled || s.pacUrl !== String(url))) {
      throw new Error('macOS가 일부 네트워크 서비스에 PAC 설정을 적용하지 않았습니다');
    }
    return after;
  }

  async function restore(serialized) {
    let previous;
    try {
      previous = JSON.parse(serialized);
    } catch {
      throw new Error('복원할 macOS 프록시 상태가 올바르지 않습니다');
    }

    const current = await query();
    const currentNames = new Set(current.services.map((s) => s.name));
    // previous가 없는 강제복구(DIRECT)면 현재 서비스에서 PAC를 최소한 끈다.
    const targets = Array.isArray(previous.services)
      ? previous.services.filter((s) => currentNames.has(s.name))
      : current.services.map((s) => ({ name: s.name, pacUrl: null, pacEnabled: false }));
    const calls = [];
    for (const service of targets) {
      calls.push(['-setautoproxyurl', service.name, service.pacUrl || '']);
      calls.push(['-setautoproxystate', service.name, service.pacEnabled ? 'on' : 'off']);
      calls.push(['-setproxyautodiscovery', service.name,
        service.autoDiscoveryEnabled ? 'on' : 'off']);
    }
    await runPrivileged(calls);
    return query();
  }

  return async function runCommand(command, arg) {
    if (command === 'query') return query();
    if (command === 'set-pac') return setPac(arg);
    if (command === 'restore') return restore(arg);
    throw new Error(`모르는 macOS system-proxy 명령: ${command}`);
  };
}

const runCommand = createRunner();

module.exports = {
  createRunner,
  runCommand,
  parseServices,
  parseAutoProxy,
  parseManualProxy,
  discoveryEnabled,
  shellQuote,
};
