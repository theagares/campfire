'use strict';
/**
 * main/system-proxy.js
 * Windows/macOS 사용자 프록시 설정을 PAC 로 바꾸고 되돌린다.
 *
 * 왜 PAC 인가 — 수동 프록시(ProxyServer)로 걸면 PC 의 **모든** 트래픽이 엔진을
 * 지난다. 그러면 엔진이 죽거나 재시작하는 동안(모델 로드로 10초 넘게 걸린다)
 * 둘 중 하나를 골라야 한다: 설정을 풀어 AI 업로드가 검사 없이 나가게 하거나,
 * 설정을 둬서 인터넷 전체를 막거나. PAC 는 AI 사이트만 프록시로 보내므로 엔진이
 * 죽으면 **AI 사이트만 막히고**(fail-closed) 나머지는 그대로 된다.
 *
 * 실제 쓰기는 Windows의 system-proxy.ps1(InternetSetOption)과 macOS의
 * system-proxy-macos.js(networksetup)가 맡는다.
 */

const path = require('path');
const { execFile } = require('child_process');
const macosProxy = require('./system-proxy-macos');

// app.asar 안의 스크립트는 powershell 이 못 연다. 패키징 시 asarUnpack 대상이다.
const SCRIPT = path.join(__dirname, 'system-proxy.ps1').replace('app.asar', 'app.asar.unpacked');

// InternetSetOption 플래그
const PROXY_TYPE_PROXY = 2;
const PROXY_TYPE_AUTO_PROXY_URL = 4;

function runScript(command, arg) {
  return new Promise((resolve, reject) => {
    const args = ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', SCRIPT, command];
    // 인자는 base64 로 넘긴다. powershell.exe 는 -File 인자의 큰따옴표를 지운다 —
    // execFile 이 제대로 이스케이프해도 {"flags":9} 가 {flags:9} 로 도착했다(실측).
    // 그러면 restore 가 매번 JSON 해석에서 죽어, 토글을 꺼도 PAC 가 안 풀린다.
    if (arg !== undefined) args.push(Buffer.from(String(arg), 'utf8').toString('base64'));
    execFile('powershell.exe', args, { windowsHide: true, timeout: 30000 }, (err, stdout, stderr) => {
      if (err) {
        reject(new Error(`system-proxy ${command} 실패: ${(stderr || err.message).trim()}`));
        return;
      }
      try {
        resolve(JSON.parse(stdout.trim()));
      } catch {
        reject(new Error(`system-proxy ${command} 출력 해석 실패: ${stdout.slice(0, 200)}`));
      }
    });
  });
}

/** 테스트는 runner 를 갈아끼운다 — 실제 사용자 설정을 건드리지 않고 로직만 본다. */
function create(runner = null, platform = null) {
  // 가짜 runner만 넘기는 기존 테스트는 Windows 모양의 상태를 사용한다. macOS 테스트는
  // create(runner, 'darwin')로 명시하고, 실제 앱은 현재 플랫폼 runner를 자동 선택한다.
  const selectedPlatform = platform || (runner ? 'win32' : process.platform);
  const selectedRunner = runner || (selectedPlatform === 'darwin' ? macosProxy.runCommand : runScript);
  const supported = !!runner || selectedPlatform === 'win32' || selectedPlatform === 'darwin';

  function macServices(state) {
    return state && Array.isArray(state.services) ? state.services : [];
  }

  function macOurs(state, ourPacPrefix) {
    return macServices(state).filter((service) => (
      service.pacEnabled && service.pacUrl && service.pacUrl.startsWith(ourPacPrefix)
    ));
  }

  return {
    supported,

    /** Windows는 단일 상태, macOS는 services 배열을 반환한다. */
    query: () => selectedRunner('query'),

    setPac: (url) => selectedRunner('set-pac', url),

    restore: (previous) => selectedRunner('restore', JSON.stringify(previous)),

    /**
     * 켜면 안 되는 이유. 없으면 null.
     *
     * 이미 다른 프록시가 있으면 손대지 않는다. 우리 PAC 는 AI 사이트 밖을 DIRECT 로
     * 보내므로, 회사 프록시·회사 PAC 위에 덮으면 그 사람의 나머지 인터넷이 회사
     * 프록시를 우회한다 — 회사 네트워크에서는 그게 곧 인터넷이 끊기는 것이다.
     */
    conflict(state, ourPacPrefix) {
      if (selectedPlatform === 'darwin' || (state && state.platform === 'darwin')) {
        for (const service of macServices(state)) {
          for (const manual of [service.web, service.secureWeb, service.socks]) {
            if (manual && manual.enabled && manual.server) {
              return `다른 프록시(${manual.server}:${manual.port || ''}, ${service.name})가 이미 설정돼 있습니다`;
            }
          }
          if (service.pacEnabled && service.pacUrl
              && !service.pacUrl.startsWith(ourPacPrefix)) {
            return `다른 자동 구성 스크립트(${service.pacUrl}, ${service.name})가 이미 설정돼 있습니다`;
          }
        }
        return null;
      }
      if (state.flags & PROXY_TYPE_PROXY && state.server) {
        return `다른 프록시(${state.server})가 이미 설정돼 있습니다`;
      }
      if (state.flags & PROXY_TYPE_AUTO_PROXY_URL && state.pacUrl
          && !state.pacUrl.startsWith(ourPacPrefix)) {
        return `다른 자동 구성 스크립트(${state.pacUrl})가 이미 설정돼 있습니다`;
      }
      return null;
    },

    /** 지금 우리 PAC 가 걸려 있는가. */
    isOurs(state, ourPacPrefix) {
      if (selectedPlatform === 'darwin' || (state && state.platform === 'darwin')) {
        // 하나라도 남았으면 복원 대상이다. 네트워크 서비스가 실행 중 추가돼 일부만
        // 우리 PAC인 상태에서도 워치독이 기존 서비스를 반드시 되돌려야 한다.
        return macOurs(state, ourPacPrefix).length > 0;
      }
      return !!(state.flags & PROXY_TYPE_AUTO_PROXY_URL
        && state.pacUrl && state.pacUrl.startsWith(ourPacPrefix));
    },

    /** UI의 "적용됨"은 모든 서비스가 보호될 때만 true다. */
    isFullyOurs(state, ourPacPrefix) {
      if (selectedPlatform === 'darwin' || (state && state.platform === 'darwin')) {
        const services = macServices(state);
        return services.length > 0 && macOurs(state, ourPacPrefix).length === services.length;
      }
      return this.isOurs(state, ourPacPrefix);
    },
  };
}

module.exports = { create, runScript, PROXY_TYPE_PROXY, PROXY_TYPE_AUTO_PROXY_URL };
