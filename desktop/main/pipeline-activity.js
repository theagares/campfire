'use strict';
/**
 * main/pipeline-activity.js
 * 엔진의 GET /activity/stream (SSE) 을 구독해 처리 단계 변화를 렌더러로 흘린다.
 *
 * 왜 메인 프로세스가 중계하나: 렌더러는 contextIsolation 아래 화이트리스트 API 만
 * 쓰고(preload.js), 엔진 포트는 기동할 때마다 탐색으로 정해진다(engine-manager).
 * 그래서 기존 metrics/stats 와 같은 방식으로 메인이 구독하고 IPC push 로 넘긴다.
 *
 * 연결은 엔진이 running 일 때만 유지한다. 끊기면 백오프를 두고 다시 붙는다 —
 * 엔진 재시작(설정 변경 등)이 흔해서 재연결이 없으면 처리현황이 조용히 죽는다.
 */

const http = require('http');
const { EventEmitter } = require('events');

const RECONNECT_MIN_MS = 1000;
const RECONNECT_MAX_MS = 8000;

// 'finish' 이벤트를 놓쳤을 때 job 이 영원히 "검사 중"으로 남지 않게 하는 상한.
// 엔진 쪽 core/activity.py 도 같은 이유로 항목마다 startedAt 을 실어 보낸다.
const JOB_MAX_AGE_MS = 5 * 60 * 1000;

/** 엔진이 요청을 받을 수 있는 상태인가. getStatus() 는 state 문자열을 준다(running 불리언 없음). */
function isEngineRunning(status) {
  return !!(status && status.state === 'running' && status.baseUrl);
}

class PipelineActivity extends EventEmitter {
  /** @param {import('./engine-manager')} engineManager */
  constructor(engineManager) {
    super();
    this.engineManager = engineManager;
    this.req = null;
    this.timer = null;
    this.backoff = RECONNECT_MIN_MS;
    this.stopped = true;
    this.buffer = '';
    // 진행 중인 job 집합. "지금 검사 중인가"만 알면 되는 소비자(트레이 불꽃)를 위해
    // 여기서 한 번만 계산해 busy 변화로 내보낸다 — 렌더러는 단계별 상세가 필요해
    // 자기 상태를 따로 들고 있고, 이쪽은 불리언 하나면 충분하다.
    this.jobs = new Map(); // jobId -> 마지막으로 소식을 들은 시각(ms)
    this.busy = false;
  }

  start() {
    if (!this.stopped) return;
    this.stopped = false;
    this.connect();
    // 엔진이 다시 떴을 때 곧바로 재구독한다(포트가 바뀌었을 수 있다).
    // 상태 판정은 engineManager.getStatus().state 를 쓴다 — running 같은 불리언
    // 필드는 없다(ipc.js 도 같은 방식).
    this.engineManager.on('status', (status) => {
      if (this.stopped) return;
      if (isEngineRunning(status) && !this.req) this.scheduleReconnect(0);
      if (status && !isEngineRunning(status)) this.disconnect();
    });
  }

  stop() {
    this.stopped = true;
    this.disconnect();
    clearTimeout(this.timer);
    this.timer = null;
  }

  disconnect() {
    if (this.req) {
      try { this.req.destroy(); } catch { /* noop */ }
      this.req = null;
    }
    this.buffer = '';
    // 연결이 끊기면 진행 상황을 더는 알 수 없다. 마지막으로 본 상태를 붙들고 있으면
    // 엔진이 재시작한 뒤에도 트레이가 계속 "검사 중"으로 타오른다.
    this._setJobs(new Map());
  }

  /** SSE 이벤트를 "지금 검사 중인가"로 접는다. */
  _track(payload) {
    if (!payload) return;
    const now = Date.now();
    if (payload.type === 'snapshot') {
      // 늦게 붙은 구독자용 첫 프레임 — 이게 진행 중인 job 의 정답이다.
      const next = new Map();
      for (const a of payload.active || []) {
        if (a && a.jobId) next.set(a.jobId, now);
      }
      this._setJobs(next);
      return;
    }
    if (payload.type !== 'activity' || !payload.jobId) return;
    if (payload.phase === 'finish') this.jobs.delete(payload.jobId);
    else this.jobs.set(payload.jobId, now);

    // finish 를 놓친 job 이 남아 영원히 타오르는 것을 막는다.
    for (const [id, seen] of this.jobs) {
      if (now - seen > JOB_MAX_AGE_MS) this.jobs.delete(id);
    }
    this._emitBusy();
  }

  _setJobs(next) {
    this.jobs = next;
    this._emitBusy();
  }

  _emitBusy() {
    const busy = this.jobs.size > 0;
    if (busy === this.busy) return;
    this.busy = busy;
    this.emit('busy', busy);
  }

  scheduleReconnect(delayMs) {
    clearTimeout(this.timer);
    if (this.stopped) return;
    const wait = delayMs != null ? delayMs : this.backoff;
    this.timer = setTimeout(() => this.connect(), wait);
    this.backoff = Math.min(this.backoff * 2, RECONNECT_MAX_MS);
  }

  connect() {
    if (this.stopped || this.req) return;
    const status = this.engineManager.getStatus();
    if (!isEngineRunning(status)) {
      this.scheduleReconnect();
      return;
    }

    const url = new URL('/activity/stream', status.baseUrl);
    const req = http.get(
      {
        host: url.hostname,
        port: url.port,
        path: url.pathname,
        headers: { Accept: 'text/event-stream' },
      },
      (res) => {
        if (res.statusCode !== 200) {
          // 구버전 엔진(엔드포인트 없음)이면 404 — 조용히 물러난다. 처리현황은
          // 그대로 idle 로 남고 앱의 나머지 기능은 영향받지 않는다.
          res.resume();
          this.req = null;
          this.scheduleReconnect();
          return;
        }
        this.backoff = RECONNECT_MIN_MS; // 붙었으니 백오프 리셋
        res.setEncoding('utf8');
        res.on('data', (chunk) => this.onChunk(chunk));
        res.on('end', () => { this.req = null; this.scheduleReconnect(); });
        res.on('error', () => { this.req = null; this.scheduleReconnect(); });
      }
    );
    req.on('error', () => { this.req = null; this.scheduleReconnect(); });
    this.req = req;
  }

  /** SSE 프레임 파싱 — 빈 줄로 구분되고, ':' 로 시작하는 줄은 keepalive 주석이다. */
  onChunk(chunk) {
    this.buffer += chunk;
    let idx;
    while ((idx = this.buffer.indexOf('\n\n')) !== -1) {
      const frame = this.buffer.slice(0, idx);
      this.buffer = this.buffer.slice(idx + 2);
      for (const line of frame.split('\n')) {
        if (!line.startsWith('data: ')) continue;
        try {
          const payload = JSON.parse(line.slice(6));
          this._track(payload);
          this.emit('activity', payload);
        } catch { /* 깨진 프레임은 버린다 */ }
      }
    }
  }
}

module.exports = { PipelineActivity };
