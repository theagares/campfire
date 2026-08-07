'use strict';
/**
 * main/engine-manager.js
 * Python 엔진(사이드카) 생명주기 관리 + /health 폴링 기반 포트 탐지 (PLAN §8·§11).
 *
 * 책임:
 *   - spawn:  <engineDir>/.venv/Scripts/python.exe -m app.main  (cwd=engineDir)
 *             인젝션 정책은 SECUREDOC_INJECTION_POLICY env 로 반영(엔진엔 REST 쓰기 없음).
 *   - 포트 탐지: 엔진이 스스로 48200~48209 중 하나에 바인딩 → 앱은 그 범위를 병렬
 *             GET /health 스캔해 service=="campfire" 인 포트를 찾는다(PLAN §11).
 *   - 상태 폴링: 주기적으로 /health → {running, port, detectors, injectionPolicy...} 브로드캐스트.
 *   - restart/stop: 정책 변경·재시작 요청 시. Windows 는 자식(uvicorn) 트리까지 종료.
 *
 * 이벤트: 'status' (EngineStatus) 를 emit — main.js 가 창/트레이로 push.
 */

const { EventEmitter } = require('events');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const http = require('http');
const { app } = require('electron');

const constants = require('./constants');
const paths = require('./paths');

/**
 * 엔진 로그를 이 프로세스의 stdout 으로 흘릴지 여부.
 *
 * 엔진(uvicorn/파이썬)은 요청마다 접근 로그를 뱉는데, 이걸 그대로 console.log 하면
 * 설치본을 터미널에서 실행했을 때 그 창에 로그가 계속 쏟아진다(실사용자 리포트:
 * "앱 실행 로그가 cmd 에 계속 뜬다"). 개발 중에는 필요한 정보라 dev 에서만 남기고,
 * 배포본에서는 끈다 — ipc.js 의 dev 판정과 같은 기준을 쓴다.
 */
const DEV = process.argv.includes('--dev') || !app?.isPackaged;

/** 비정상 종료 메시지에 붙일 단서를 뽑기 위해 들고 있는 최근 엔진 로그 줄 수. */
const RECENT_LOG_LINES = 80;
/** 엔진 로그 파일 상한 — 넘으면 비우고 다시 쓴다(접근 로그가 계속 쌓인다). */
const ENGINE_LOG_MAX_BYTES = 2 * 1024 * 1024;

/**
 * @typedef {Object} EngineStatus
 * @property {'stopped'|'starting'|'running'|'error'|'disabled'} state
 * @property {number|null} port
 * @property {string|null} baseUrl
 * @property {Object|null} health   // /health 응답 원본
 * @property {string|null} message
 */

class EngineManager extends EventEmitter {
  constructor(app, configStore) {
    super();
    this.app = app;
    this.config = configStore;
    this.child = null;
    this.boundPort = null;
    this.pollTimer = null;
    this.state = 'stopped';
    this.lastHealth = null;
    this.message = null;
    this.restartAttempts = 0;
    this.maxRestartAttempts = 3;
    this.intentionalStop = false;
    this.recentLog = [];
    this.engineDir = paths.resolveEngineDir(app);
    this.pythonExe = paths.resolvePythonExe(this.engineDir);
  }

  getStatus() {
    return {
      state: this.state,
      port: this.boundPort,
      baseUrl: this.boundPort ? `http://${constants.HOST}:${this.boundPort}` : null,
      health: this.lastHealth,
      message: this.message,
      engineDir: this.engineDir,
      pythonExe: this.pythonExe,
    };
  }

  _setState(state, message) {
    this.state = state;
    this.message = message || null;
    this.emit('status', this.getStatus());
  }

  /** 사이드카 spawn. securityEnabled=false 면 띄우지 않고 disabled 상태로 둔다. */
  async start() {
    if (!this.config.get('securityEnabled')) {
      this._setState('disabled', '보안 보호가 꺼져 있습니다 (트레이에서 켜기)');
      return;
    }
    if (this.child) {
      return; // 이미 실행 중
    }
    this.intentionalStop = false;

    const diag = paths.diagnose(this.engineDir, this.pythonExe);
    if (!diag.pythonExeExists || !diag.appMainExists) {
      this._setState(
        'error',
        `엔진을 찾을 수 없습니다. python=${diag.pythonExeExists ? 'ok' : '없음'} ` +
          `app/main.py=${diag.appMainExists ? 'ok' : '없음'} (dir=${this.engineDir})`
      );
      return;
    }

    this._setState('starting', '엔진 사이드카를 기동합니다...');

    const env = {
      ...process.env,
      // PLAN §4/§8: 인젝션 정책을 spawn 시점에 반영. 엔진 config.py 가 이 env 를 읽는다.
      SECUREDOC_INJECTION_POLICY: this.config.get('injectionPolicy') || 'mask',
      // config-store 의 piiDetector/injectionDetector 를 실제로 엔진에 반영. 룰베이스
      // 폴백을 완전히 제거한 뒤에는 encoder/llm_mcp 가 유일한 값이다 — 모델 가중치는
      // 설치 파일에 없고 설치 후 자동 다운로드되지만(MODELS.md), 가중치가 아직 없어도
      // 엔진 자체는 정상 기동하고 검사 시점에 model_status 게이트가 통과 처리한다.
      SECUREDOC_PII_DETECTOR: this.config.get('piiDetector') || 'encoder',
      SECUREDOC_INJECTION_DETECTOR: this.config.get('injectionDetector') || 'llm_mcp',
      // Upstage Solar API 키. 설정 화면(#upstage-api-key)에서 저장한 값을 그대로
      // 전달한다 — 없으면 빈 문자열이고, 엔진 config.py 가 이를 os.environ.get(...) or ""
      // 로 안전하게 받아 INJECTION_LOCALIZE_ENABLED=False 로 처리한다(별도 분기 불필요).
      SECUREDOC_UPSTAGE_API_KEY: this.config.get('upstageApiKey') || '',
      PYTHONUNBUFFERED: '1',
      PYTHONUTF8: '1',
      // ── 앱 번들에 아무것도 쓰지 않게 한다 (macOS 에서 앱을 못 쓰게 만들던 원인) ──
      //
      // 실사용자 mac(0.2.17, 신규 설치)에서 확인된 것:
      //   codesign --verify → "file added: .../.venv/lib/python3.11/re/__pycache__/*.pyc"
      //   /Applications/Campfire.app/.../engine/app/store/data/securedoc.sqlite3 존재
      //   xattr → com.apple.quarantine 살아 있음
      // 빌드가 __pycache__ 를 빼고 패키징하므로 첫 실행 때 Python 이 stdlib 을 컴파일하며
      // 번들 안에 .pyc 를 쓰고, 엔진은 DB 를 번들 안에 쓴다. 둘 다 서명 이후의 파일이라
      // ad-hoc 서명(진짜 인증서가 없어 codesign --sign - 로만 서명한다)이 첫 실행에
      // 스스로 깨진다. 격리 속성까지 살아 있으면 Gatekeeper 가 개입하고, Apple Silicon 은
      // 서명이 깨진 바이너리 실행을 거부할 수 있다.
      // Windows 에서 같은 코드가 멀쩡했던 건 번들 서명 검증이 없어서일 뿐이다.
      PYTHONPYCACHEPREFIX: paths.resolvePycacheDir(),
      SECUREDOC_STORE_DIR: paths.resolveStoreDir(),
      // cwd 를 번들 밖으로 뺐으므로 app 패키지를 PYTHONPATH 로 알려준다(아래 주석).
      PYTHONPATH: this.engineDir,
    };

    try {
      // cwd 를 engineDir(=앱 번들 내부)로 잡지 않는다.
      //
      // 번들이 교체·재배치되면 실행 중인 엔진의 CWD 가 사라지고, 그러면 Python 은
      // import 도중 os.getcwd() 에서 죽는다. 실사용자 트레이스백이 정확히 그 모양이었다:
      //   import torch → inspect.getmodule → getabsfile → posixpath.abspath
      //   → FileNotFoundError: [Errno 2]
      // abspath 가 ENOENT 를 내는 경로는 내부의 os.getcwd() 뿐이다. 엔진이 안 뜨니
      // 마스킹 결정이 안 나오고, 사용자 눈에는 "사이트에서 아무 일도 안 일어남" 으로만
      // 보였다.
      //
      // 그래서 항상 존재하는 사용자 데이터 폴더를 cwd 로 쓰고, app 패키지는
      // PYTHONPATH 로 찾게 한다(`-m app.main` 은 sys.path 에서 찾으므로 동작이 같다).
      // 엔진은 경로를 전부 __file__ 기준 절대경로로 잡으므로 cwd 에 의존하지 않는다.
      const cwd = paths.userDataRoot();
      try { fs.mkdirSync(cwd, { recursive: true }); } catch (_) { /* 아래 spawn 이 알려준다 */ }

      this.child = spawn(this.pythonExe, ['-m', 'app.main'], {
        cwd,
        env,
        windowsHide: true,
        stdio: ['ignore', 'pipe', 'pipe'],
      });
    } catch (err) {
      this.child = null;
      this._setState('error', `엔진 spawn 실패: ${err.message}`);
      return;
    }

    this.child.stdout.on('data', (d) => this._onEngineLog(d));
    this.child.stderr.on('data', (d) => this._onEngineLog(d));
    this.child.on('exit', (code, signal) => this._onExit(code, signal));
    this.child.on('error', (err) => {
      this._setState('error', `엔진 프로세스 오류: ${err.message}`);
    });

    // 엔진이 포트를 바인딩할 때까지 /health 스캔으로 대기 후, 주기 폴링 시작.
    this._startPolling();
  }

  /** 엔진이 뱉은 것을 기록한다.
   *
   *  예전엔 배포본에서 이걸 통째로 버렸다(`if (!DEV) return`). 그래서 실사용자 mac 에서
   *  엔진이 import 도중 죽었을 때 앱에는 흔적이 하나도 안 남았고, 증상은 "사이트에서
   *  아무 일도 안 일어남" 뿐이었다 — 앱을 터미널에서 직접 띄우지 않았으면 원인을 영영
   *  못 찾았을 것이다. 터미널로 쏟아내지 않는다는 원래 의도(설치본을 cmd 에서 실행했을
   *  때 로그가 계속 뜨던 문제)는 그대로 지키면서, 파일과 메모리에는 남긴다. */
  _onEngineLog(buf) {
    const text = buf.toString('utf-8');
    const line = text.trim();
    if (!line) return;

    if (DEV) console.log('[engine]', line);

    // 최근 줄은 메모리에 — 비정상 종료 시 사용자에게 보여줄 근거로 쓴다.
    for (const l of line.split('\n')) {
      this.recentLog.push(l);
    }
    if (this.recentLog.length > RECENT_LOG_LINES) {
      this.recentLog.splice(0, this.recentLog.length - RECENT_LOG_LINES);
    }

    this._appendLogFile(text);
  }

  _appendLogFile(text) {
    try {
      const p = paths.resolveEngineLogPath();
      fs.mkdirSync(path.dirname(p), { recursive: true });
      // 무한히 자라지 않게 한다. uvicorn 접근 로그가 계속 쌓이므로 상한이 필요하다.
      try {
        if (fs.statSync(p).size > ENGINE_LOG_MAX_BYTES) fs.rmSync(p, { force: true });
      } catch (_) { /* 파일이 아직 없다 */ }
      fs.appendFileSync(p, text.endsWith('\n') ? text : `${text}\n`);
    } catch (_) {
      // 로그를 못 남기는 것으로 엔진 기동을 막지 않는다.
    }
  }

  /** 비정상 종료를 사용자에게 설명할 때 붙일 마지막 단서. */
  _lastErrorLines(n = 3) {
    const meaningful = this.recentLog
      .map((l) => l.trim())
      .filter(Boolean)
      .filter((l) => !/^INFO:/.test(l)); // 평범한 기동 로그는 단서가 못 된다
    return meaningful.slice(-n).join(' / ');
  }

  _onExit(code, signal) {
    if (DEV) console.log(`[engine] 종료 (code=${code}, signal=${signal})`);
    this.child = null;
    this.boundPort = null;
    this.lastHealth = null;

    if (this.intentionalStop) {
      this._setState('stopped', '엔진이 중지되었습니다');
      return;
    }
    // 비정상 종료 → 제한된 횟수만 자동 재시작 (crash loop 방지)
    if (this.restartAttempts < this.maxRestartAttempts) {
      this.restartAttempts += 1;
      this._setState('starting', `엔진이 예기치 않게 종료됨 — 재시작 시도 ${this.restartAttempts}/${this.maxRestartAttempts}`);
      setTimeout(() => this.start(), 1200 * this.restartAttempts);
    } else {
      // 무엇 때문에 죽었는지를 화면에 그대로 붙인다 — "로그를 확인하세요" 만으로는
      // 사용자가 확인할 로그가 어디에도 없었다.
      const clue = this._lastErrorLines();
      this._setState(
        'error',
        `엔진이 반복적으로 종료됩니다${clue ? ` — ${clue}` : ''} `
        + `(로그: ${paths.resolveEngineLogPath()})`,
      );
    }
  }

  // ── 포트 탐지 & 상태 폴링 (PLAN §11) ────────────────────────────────────────
  _startPolling() {
    this._stopPolling();
    const tick = async () => {
      const found = await this._scanForEngine();
      if (found) {
        this.restartAttempts = 0; // 정상 응답 확인 → 재시작 카운터 리셋
        this.boundPort = found.port;
        this.lastHealth = found.health;
        if (this.state !== 'running') {
          this._setState('running', null);
        } else {
          this.emit('status', this.getStatus());
        }
      } else if (this.child && this.state === 'starting') {
        // 아직 기동 중 — 계속 대기
      } else if (this.child) {
        // 프로세스는 살아있으나 /health 미응답 → 무응답 표시(자동종료는 안 함)
        this._setState('error', '엔진 프로세스는 있으나 /health 에 응답하지 않습니다');
      }
    };
    tick();
    this.pollTimer = setInterval(tick, constants.HEALTH_POLL_INTERVAL_MS);
  }

  _stopPolling() {
    if (this.pollTimer) {
      clearInterval(this.pollTimer);
      this.pollTimer = null;
    }
  }

  /**
   * 48200~48209 를 병렬 GET /health 스캔. service 시그니처가 일치하는 가장 낮은
   * 포트를 채택(PLAN §11). 캐시된 포트가 있으면 그 포트를 먼저 확인해 스캔을 줄인다.
   * @returns {Promise<{port:number, health:object}|null>}
   */
  async _scanForEngine() {
    // 1) 캐시된 포트 우선 확인 (있고 여전히 우리 엔진이면 즉시 채택)
    if (this.boundPort) {
      const h = await this._probe(this.boundPort);
      if (h) return { port: this.boundPort, health: h };
      // 실패 → 포트가 바뀌었을 수 있으니 전체 재스캔 (PLAN §11)
    }
    // 2) 전체 범위 병렬 스캔
    const ports = [];
    for (let i = 0; i < constants.PORT_SCAN_COUNT; i++) {
      ports.push(constants.BASE_PORT + i);
    }
    const results = await Promise.all(
      ports.map(async (p) => ({ port: p, health: await this._probe(p) }))
    );
    const matches = results.filter((r) => r.health);
    if (matches.length === 0) return null;
    matches.sort((a, b) => a.port - b.port); // 방어적으로 가장 낮은 포트 우선
    return matches[0];
  }

  /** 단일 포트 /health 확인. 시그니처 불일치/타임아웃 시 null. */
  _probe(port) {
    return new Promise((resolve) => {
      const req = http.get(
        { host: constants.HOST, port, path: '/health', timeout: constants.HEALTH_TIMEOUT_MS },
        (res) => {
          if (res.statusCode !== 200) {
            res.resume();
            return resolve(null);
          }
          let body = '';
          res.setEncoding('utf-8');
          res.on('data', (c) => (body += c));
          res.on('end', () => {
            try {
              const json = JSON.parse(body);
              // 반드시 시그니처 일치해야 "우리 엔진" (PLAN §11: 우연히 뜬 다른 서버 오판 방지)
              if (json && (json.service === constants.SERVICE_NAME
                || constants.LEGACY_SERVICE_NAMES.includes(json.service))) {
                return resolve(json);
              }
            } catch {
              /* ignore */
            }
            resolve(null);
          });
        }
      );
      req.on('timeout', () => {
        req.destroy();
        resolve(null);
      });
      req.on('error', () => resolve(null));
    });
  }

  // ── 제어 ────────────────────────────────────────────────────────────────────
  /** 정책 변경 등으로 엔진 재시작 (env 를 다시 적용). */
  async restart() {
    this.restartAttempts = 0;
    await this.stop();
    // 종료가 반영될 시간을 잠깐 준 뒤 재기동
    await new Promise((r) => setTimeout(r, 400));
    await this.start();
  }

  /** 엔진 종료. Windows 는 uvicorn 자식까지 트리 종료. */
  stop() {
    this.intentionalStop = true;
    this._stopPolling();
    return new Promise((resolve) => {
      const child = this.child;
      if (!child) {
        this.boundPort = null;
        this.lastHealth = null;
        resolve();
        return;
      }
      const done = () => {
        this.boundPort = null;
        this.lastHealth = null;
        resolve();
      };
      child.once('exit', done);

      if (process.platform === 'win32' && child.pid) {
        // 트리 종료 (uvicorn 워커 포함)
        try {
          spawn('taskkill', ['/pid', String(child.pid), '/T', '/F'], { windowsHide: true });
        } catch {
          try { child.kill(); } catch { /* noop */ }
        }
      } else {
        try { child.kill('SIGTERM'); } catch { /* noop */ }
        setTimeout(() => {
          if (this.child) {
            try { this.child.kill('SIGKILL'); } catch { /* noop */ }
          }
        }, 2500);
      }
      // 안전장치: 5초 내 exit 이벤트 없으면 그냥 진행
      setTimeout(done, 5000);
    });
  }

  /** 트레이 Security ON/OFF: ON=엔진 가동, OFF=엔진 중지 (실제 보호 상태와 일치). */
  async setSecurityEnabled(enabled) {
    this.config.set({ securityEnabled: !!enabled });
    if (enabled) {
      this.restartAttempts = 0;
      await this.start();
    } else {
      await this.stop();
      this._setState('disabled', '보안 보호가 꺼졌습니다');
    }
  }

  async dispose() {
    this._stopPolling();
    await this.stop();
  }
}

module.exports = { EngineManager };
