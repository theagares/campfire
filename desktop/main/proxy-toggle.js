'use strict';
/**
 * main/proxy-toggle.js
 * 프록시 토글 하나로 세 가지를 같이 움직인다: 엔진 프록시 · PAC 서버 · 시스템 설정.
 *
 * 따로 움직이면 두 가지 사고가 난다.
 *   - 시스템만 켜지고 엔진 프록시가 없다 → 브라우저가 죽은 포트를 향한다
 *   - 엔진만 꺼지고 시스템이 그대로다   → 같은 결과
 * 그래서 켜는 순서와 끄는 순서를 여기 한 곳에 둔다.
 *
 * 켜기: 엔진 프록시 → (실제로 듣는지·CA 가 신뢰되는지 확인) → PAC → 시스템 설정
 * 끄기: 시스템 설정 → PAC → 엔진 프록시        (브라우저를 먼저 돌려놓는다)
 *
 * 저장하는 상태는 둘뿐이다(config):
 *   proxyEnabled        — 사용자가 켜 두길 원하는가
 *   proxySystemPrevious — 우리가 덮어쓰기 **전의** 시스템 설정. null 이면 안 덮어썼다.
 * 뒤의 것은 켜기 직전에 디스크에 먼저 쓴다. 앱이 강제 종료돼도 다음 실행 때
 * 이 값으로 되돌린다 — 안 그러면 사용자 브라우저가 없는 PAC 서버를 계속 찾는다.
 */

function create({
  config, engineManager, systemProxy, pacServer, fetchImpl = fetch,
  // 강제종료(작업관리자·크래시)로 cleanup 이 못 돌 때 시스템 프록시를 되돌리는
  // 감시 프로세스. 기본은 no-op — main.js 가 실제 host 를 주입한다(테스트는 스텁).
  watchdog = { arm() {}, disarm() {} },
}) {
  let lastError = null;
  let busy = null; // 켜기/끄기가 겹치지 않게 한 줄로 세운다

  function baseUrl() {
    return engineManager.getStatus().baseUrl || null;
  }

  async function engine(method, path) {
    const base = baseUrl();
    if (!base) throw new Error('엔진이 실행 중이 아닙니다');
    const res = await fetchImpl(`${base}${path}`, { method });
    if (!res.ok) throw new Error(`엔진 ${path} 응답 ${res.status}`);
    return res.json();
  }

  function serial(fn) {
    const run = (busy || Promise.resolve()).then(fn);
    // 꼬리는 실패를 삼킨다 — 앞 작업이 실패해도 다음 작업은 돌아야 하고, 아무도
    // 기다리지 않는 실패가 unhandled rejection 으로 남지 않게.
    const tail = run.catch(() => {});
    busy = tail;
    tail.then(() => { if (busy === tail) busy = null; });
    return run;
  }

  async function restoreSystem() {
    const prev = config.get('proxySystemPrevious');
    if (!prev) return;
    await systemProxy.restore(prev);
    config.set({ proxySystemPrevious: null });
  }

  async function enableNow() {
    if (!systemProxy.supported) {
      return fail('unsupported', 'Windows 에서만 지원합니다');
    }
    let st;
    try {
      st = await engine('POST', '/proxy/start');
    } catch (err) {
      return fail('engine', err.message);
    }
    if (!st.running) {
      return fail('engine', `프록시가 ${st.port} 포트를 붙잡지 못했습니다`);
    }
    if (st.caTrusted !== true) {
      // CA 없이 브라우저를 돌리면 AI 사이트가 전부 인증서 오류로 막힌다.
      // 엔진 프록시는 내린다 — 아무도 안 오는 프록시를 켜 둘 이유가 없다.
      await engine('POST', '/proxy/stop').catch(() => {});
      return fail('ca', 'CA 인증서가 신뢰 저장소에 없습니다', { caPath: st.caPath });
    }

    const current = await systemProxy.query();
    const conflict = systemProxy.conflict(current, pacServer.PAC_PREFIX);
    if (conflict) {
      await engine('POST', '/proxy/stop').catch(() => {});
      return fail('conflict', conflict);
    }

    const pacUrl = await pacServer.publish(st.hosts, st.port);
    // 되돌릴 값을 **먼저** 디스크에 쓴다. 이미 우리가 걸어 둔 상태라면(재적용)
    // 덮어쓰지 않는다 — 그러면 원래 값을 잃고 우리 PAC 를 "원래 값" 으로 기억한다.
    if (!config.get('proxySystemPrevious') && !systemProxy.isOurs(current, pacServer.PAC_PREFIX)) {
      config.set({ proxySystemPrevious: current });
    }
    await systemProxy.setPac(pacUrl);
    config.set({ proxyEnabled: true });
    // 강제종료 대비: 앱이 죽으면(정상이든 강제든) 프록시를 되돌릴 감시 프로세스를
    // 띄운다. 정상 종료 땐 아래 disableNow 가 먼저 복원·disarm 하므로 겹치지 않는다.
    watchdog.arm({
      pid: process.pid,
      pacPrefix: pacServer.PAC_PREFIX,
      previous: config.get('proxySystemPrevious'),
    });
    lastError = null;
    return { ok: true };
  }

  async function disableNow({ keepDesired }) {
    // 브라우저부터 돌려놓는다. 거꾸로 하면 엔진 프록시가 내려간 뒤 설정이 풀리기
    // 전까지 AI 사이트가 죽은 포트를 향한다.
    let restoreError = null;
    try {
      await restoreSystem();
    } catch (err) {
      restoreError = err;
    }
    // 시스템을 되돌린 **뒤** 감시 프로세스를 내린다 — 순서를 지켜야, 복원 도중
    // 강제종료돼도 워치독이 남아 마저 되돌린다.
    watchdog.disarm();
    await pacServer.stop();
    await engine('POST', '/proxy/stop').catch(() => {});
    if (!keepDesired) config.set({ proxyEnabled: false });
    if (restoreError) return fail('restore', restoreError.message);
    lastError = null;
    return { ok: true };
  }

  function fail(code, message, extra = {}) {
    lastError = { code, message, ...extra };
    return { ok: false, ...lastError };
  }

  return {
    /** 토글 ON/OFF. */
    set(enabled) {
      return serial(() => (enabled ? enableNow() : disableNow({ keepDesired: false })));
    },

    /** 앱 종료·보안 OFF — 시스템은 돌려놓되 사용자 선택(켜짐)은 기억한다. */
    suspend() {
      return serial(() => disableNow({ keepDesired: true }));
    },

    /**
     * 앱을 켤 때, 엔진보다 먼저. 지난번에 되돌리지 못하고 죽었으면 지금 되돌린다.
     * PAC 서버가 아직 없으므로 이걸 안 하면 브라우저는 없는 PAC 를 찾는다.
     */
    recoverOnLaunch() {
      return serial(() => restoreSystem().catch((err) => fail('restore', err.message)));
    },

    /**
     * 엔진이 떴을 때마다 부른다. 켜 두길 원하는데 아직 안 걸려 있으면 건다.
     * 엔진 재시작으로는 PAC 를 풀지 않는다 — 엔진 프록시는 환경변수로 다시 뜨고,
     * 그 사이 AI 사이트는 죽은 포트로 가서 막힌다(fail-closed). 그게 의도다.
     */
    resumeIfDesired() {
      if (!config.get('proxyEnabled') || pacServer.isRunning()) return Promise.resolve(null);
      return serial(() => (pacServer.isRunning() ? null : enableNow()));
    },

    async status() {
      let engineProxy = null;
      try {
        engineProxy = await engine('GET', '/proxy/status');
      } catch { /* 엔진 꺼짐 */ }
      let system = null;
      if (systemProxy.supported) {
        try {
          const q = await systemProxy.query();
          system = { ...q, ours: systemProxy.isOurs(q, pacServer.PAC_PREFIX) };
        } catch (err) {
          system = { error: err.message };
        }
      }
      return {
        supported: systemProxy.supported,
        desired: !!config.get('proxyEnabled'),
        engine: engineProxy,
        pacServing: pacServer.isRunning(),
        system,
        lastError,
      };
    },
  };
}

module.exports = { create };
