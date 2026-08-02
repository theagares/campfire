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
    };

    try {
      this.child = spawn(this.pythonExe, ['-m', 'app.main'], {
        cwd: this.engineDir,
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

  _onEngineLog(buf) {
    if (!DEV) return; // 배포본에서는 터미널로 흘리지 않는다(위 DEV 주석 참고)
    const line = buf.toString('utf-8').trim();
    if (line) {
      console.log('[engine]', line);
    }
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
      this._setState('error', '엔진이 반복적으로 종료됩니다. 로그를 확인하세요.');
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
