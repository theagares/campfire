'use strict';
/**
 * main/system-proxy.js
 * Windows 사용자 프록시 설정을 PAC 로 바꾸고 되돌린다.
 *
 * 왜 PAC 인가 — 수동 프록시(ProxyServer)로 걸면 PC 의 **모든** 트래픽이 엔진을
 * 지난다. 그러면 엔진이 죽거나 재시작하는 동안(모델 로드로 10초 넘게 걸린다)
 * 둘 중 하나를 골라야 한다: 설정을 풀어 AI 업로드가 검사 없이 나가게 하거나,
 * 설정을 둬서 인터넷 전체를 막거나. PAC 는 AI 사이트만 프록시로 보내므로 엔진이
 * 죽으면 **AI 사이트만 막히고**(fail-closed) 나머지는 그대로 된다.
 *
 * 실제 쓰기는 system-proxy.ps1 이 한다(InternetSetOption — 레지스트리 문자열 값만
 * 고치면 WinHTTP 가 무시할 수 있어서, 설정 화면과 같은 API 를 쓴다).
 */

const path = require('path');
const { execFile } = require('child_process');

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
function create(runner = runScript) {
  const supported = runner !== runScript || process.platform === 'win32';

  return {
    supported,

    /** @returns {Promise<{flags:number, server:string|null, bypass:string|null, pacUrl:string|null}>} */
    query: () => runner('query'),

    setPac: (url) => runner('set-pac', url),

    restore: (previous) => runner('restore', JSON.stringify(previous)),

    /**
     * 켜면 안 되는 이유. 없으면 null.
     *
     * 이미 다른 프록시가 있으면 손대지 않는다. 우리 PAC 는 AI 사이트 밖을 DIRECT 로
     * 보내므로, 회사 프록시·회사 PAC 위에 덮으면 그 사람의 나머지 인터넷이 회사
     * 프록시를 우회한다 — 회사 네트워크에서는 그게 곧 인터넷이 끊기는 것이다.
     */
    conflict(state, ourPacPrefix) {
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
      return !!(state.flags & PROXY_TYPE_AUTO_PROXY_URL
        && state.pacUrl && state.pacUrl.startsWith(ourPacPrefix));
    },
  };
}

module.exports = { create, runScript, PROXY_TYPE_PROXY, PROXY_TYPE_AUTO_PROXY_URL };
