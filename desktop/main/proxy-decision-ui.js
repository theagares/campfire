'use strict';
/**
 * 프록시가 보류한 검사 결과를 네이티브 확인창으로 보여준다.
 * 대시보드가 트레이에 숨겨진 상태에서도 사람이 안전하게 결정할 수 있다.
 *
 * 맥에서는 확인창을 대시보드 창에 붙인다(시트). 부모 창 없는 메시지 박스는 맥에서 동기로
 * 돌아 그동안 앱 메인 프로세스 전체가 멈추고, 닫으라는 신호(AbortSignal)도 듣지 않는다
 * (Electron dialog 문서). 맥 실측: 확인창이 떠 있는 동안 앱이 응답하지 않았고, 판단이
 * 만료돼도 창이 남았다.
 */

// 오류 뒤 재연결 간격. 정상 상태에서는 이 간격으로 폴링하지 않고 엔진의 목록 버전
// 변화가 생길 때까지 한 HTTP 요청을 최대 25초 유지한다.
const POLL_MS = 1000;
const LONG_POLL_MS = 25000;
/** 확인창이 떠 있는 동안 그 판단 건이 아직 엔진에 있는지 보는 간격. */
const WATCH_MS = 1000;
/** 결정한 건을 기억하는 시간 — 엔진이 아직 목록에 두고 있어도 다시 띄우지 않는다. */
const HANDLED_TTL_MS = 10 * 60 * 1000;

class ProxyDecisionController {
  constructor({
    engineManager, dialog, fetchImpl = globalThis.fetch, pollMs = POLL_MS,
    watchMs = WATCH_MS, getParentWindow = () => null, platform = process.platform,
    autoSendMasked = () => false, // 설정 "마스킹본 바로 전송" — 켜면 묻지 않고 마스킹본을 보낸다
  }) {
    this.autoSendMasked = autoSendMasked;
    this.engineManager = engineManager;
    this.dialog = dialog;
    this.fetch = fetchImpl;
    this.pollMs = pollMs;
    this.watchMs = watchMs;
    this.getParentWindow = getParentWindow;
    this.platform = platform;
    this.timer = null;
    this.requestAbort = null;
    this.version = null;
    this.onEngineStatus = null;
    this.stopped = true;
    this.currentId = null;
    // id -> { action, at, posted }. 결정을 엔진에 못 보냈으면(posted:false) 창 없이 다시 보낸다.
    // 예전엔 전송이 실패하면 같은 건을 다시 띄워, 사용자가 "취소" 를 눌러도 창이 계속 떴다(맥 실측).
    this.handled = new Map();
  }

  start() {
    if (!this.stopped) return;
    this.stopped = false;
    this.onEngineStatus = (status) => {
      if (this.stopped) return;
      if (!status || status.state !== 'running' || !status.baseUrl) {
        this.requestAbort?.abort();
        return;
      }
      if (!this.timer && !this.requestAbort && !this.currentId) this._schedule(0);
    };
    this.engineManager.on?.('status', this.onEngineStatus);
    this._schedule(0);
  }

  stop() {
    this.stopped = true;
    clearTimeout(this.timer);
    this.timer = null;
    this.requestAbort?.abort();
    this.requestAbort = null;
    if (this.onEngineStatus) this.engineManager.off?.('status', this.onEngineStatus);
    this.onEngineStatus = null;
  }

  _schedule(delay = 0) {
    clearTimeout(this.timer);
    if (this.stopped) return;
    this.timer = setTimeout(async () => {
      this.timer = null;
      const status = this.engineManager.getStatus();
      // 엔진이 다시 뜨면 status 이벤트가 깨운다. 내려간 동안 타이머로 상태를 재조회하지 않는다.
      if (!status || status.state !== 'running' || !status.baseUrl) return;
      try {
        await this.checkNow(true);
        // 함께 실행된 구버전 엔진이 version을 주지 않으면 즉시 재귀 조회하지 않는다.
        // 현재 엔진은 version 장기 대기로 들어가고, 구버전만 낮은 빈도의 호환 폴백을 쓴다.
        this._schedule(Number.isInteger(this.version) ? 0 : this.pollMs);
      } catch (err) {
        if (!this.stopped && process.env.SECUREDOC_PROXY_DECISION_DEBUG) {
          console.warn('[proxy-decision] 조회 실패:', err.message);
        }
        const current = this.engineManager.getStatus();
        if (!this.stopped && current?.state === 'running' && current?.baseUrl) {
          this._schedule(this.pollMs);
        }
      }
    }, delay);
  }

  async _json(baseUrl, path, options = {}) {
    const signal = options.signal || (typeof AbortSignal?.timeout === 'function'
      ? AbortSignal.timeout(2000)
      : undefined);
    const res = await this.fetch(`${baseUrl}${path}`, { ...options, signal });
    if (!res.ok) {
      const err = new Error(`엔진 ${path} 응답 ${res.status}`);
      err.status = res.status;
      throw err;
    }
    return res.json();
  }

  async checkNow(waitForChange = false) {
    if (this.currentId) return false;
    const status = this.engineManager.getStatus();
    if (!status || status.state !== 'running' || !status.baseUrl) return false;

    const now = Date.now();
    for (const [id, h] of this.handled) if (now - h.at > HANDLED_TTL_MS) this.handled.delete(id);
    for (const [id, h] of this.handled) {
      if (!h.posted) await this._post(status.baseUrl, id, h.action);
    }

    let listPath = '/decisions';
    let timeout = null;
    if (waitForChange && Number.isInteger(this.version)) {
      listPath += `?after=${this.version}&wait_ms=${LONG_POLL_MS}`;
      const ctrl = new AbortController();
      this.requestAbort = ctrl;
      timeout = setTimeout(() => ctrl.abort(), LONG_POLL_MS + 5000);
    }
    let listed;
    try {
      listed = await this._json(status.baseUrl, listPath, this.requestAbort
        ? { signal: this.requestAbort.signal }
        : {});
    } finally {
      if (timeout) clearTimeout(timeout);
      this.requestAbort = null;
    }
    if (Number.isInteger(listed?.version)) this.version = listed.version;
    const pending = Array.isArray(listed?.pending) ? listed.pending : [];
    const summary = pending.find((p) => p?.id && !this.handled.has(p.id));
    if (!summary) return false;

    this.currentId = summary.id;
    try {
      await this._review(status.baseUrl, summary);
    } finally {
      this.currentId = null;
    }
    return true;
  }

  /** 결정을 엔진에 보낸다. 이미 끝난 건(404/409)도 보낸 것으로 친다. 그 밖의 실패는 다음에 다시. */
  async _post(baseUrl, id, action) {
    try {
      await this._json(baseUrl, `/decisions/${encodeURIComponent(id)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action }),
      });
    } catch (err) {
      if (err.status !== 404 && err.status !== 409) return false;
    }
    const h = this.handled.get(id);
    if (h) h.posted = true;
    return true;
  }

  /** 맥은 대시보드 창에 붙여 띄운다(앱을 막지 않고, signal 로 닫을 수 있다). */
  _show(options, signal) {
    const parent = this.platform === 'darwin' ? this.getParentWindow() : null;
    const opts = { ...options, signal };
    return parent ? this.dialog.showMessageBox(parent, opts) : this.dialog.showMessageBox(opts);
  }

  async _review(baseUrl, summary) {
    const id = summary.id;
    const path = `/decisions/${encodeURIComponent(id)}`;
    let detail = summary;
    try {
      detail = await this._json(baseUrl, path);
    } catch (err) {
      if (err.status === 404 || err.status === 409) return;
    }

    // 창이 떠 있는 동안 판단 건이 엔진에서 사라지면(브라우저가 요청을 버림·시간 초과) 창을 닫는다.
    const ctrl = new AbortController();
    let gone = false;
    const watch = setInterval(() => {
      this._json(baseUrl, path).catch((err) => {
        if (err.status === 404 || err.status === 409) { gone = true; ctrl.abort(); }
      });
    }, this.watchMs);

    let action;
    try {
      action = await this._ask(detail, ctrl.signal);
    } finally {
      clearInterval(watch);
    }
    if (gone) {
      this.handled.set(id, { action: 'cancel', at: Date.now(), posted: true });
      return;
    }
    this.handled.set(id, { action, at: Date.now(), posted: false });
    await this._post(baseUrl, id, action);
  }

  async _ask(detail, signal) {
    const pii = Number(detail.piiCount || 0);
    const injection = Number(detail.injectionCount || 0);
    const forced = Number(detail.forcedMaskCount || 0);
    const findings = pii + injection + forced;
    const result = detail.result || {};
    const complete = detail.scanStatus === 'ok' && !result.truncated;
    const hasMandatoryMask = forced > 0 || (result.forcedMaskItems || []).length > 0;
    // 다 검사했고 찾은 게 있으면 창 없이 마스킹본을 보낸다. 검사를 끝내지 못한 건은 마스킹본이
    // 온전하다고 볼 수 없어 설정과 관계없이 아래 확인창으로 묻는다.
    if (findings > 0 && complete && this.autoSendMasked()) return 'send_masked';
    const lines = [
      `대상: ${detail.host || '알 수 없음'}`,
      `개인정보 ${pii}건 · 프롬프트 인젝션 ${injection}건 · 사용자 지정 ${forced}건`,
    ];
    if (hasMandatoryMask) lines.push('사용자 지정 항목은 원본으로 전송할 수 없습니다.');
    if (!complete) {
      lines.push(`검사 상태: ${detail.scanStatus || '확인 불가'}`);
      if (result.reason) lines.push(String(result.reason));
      if (result.truncated) lines.push('문서 일부만 검사되었습니다.');
    }
    lines.push('', findings
      ? '마스킹본 전송이 기본 선택입니다.'
      : '검사를 완료하지 못해 자동 전송하지 않았습니다.');

    let action;
    if (findings > 0 && complete) {
      const buttons = hasMandatoryMask
        ? ['마스킹본 전송', '취소']
        : ['마스킹본 전송', '취소', '원본 전송'];
      const { response } = await this._show({
        type: 'warning',
        title: 'Campfire 전송 검토',
        message: `${detail.fileName || '파일'}에서 위험 요소를 찾았습니다.`,
        detail: lines.join('\n'),
        buttons,
        defaultId: 0,
        cancelId: 1,
        noLink: true,
      }, signal);
      action = response === 0 ? 'send_masked'
        : (!hasMandatoryMask && response === 2 ? 'send_original' : 'cancel');
    } else {
      const buttons = hasMandatoryMask ? ['취소', '마스킹본 전송'] : ['취소', '원본 전송'];
      const { response } = await this._show({
        type: 'warning',
        title: 'Campfire 검사 미완료',
        message: `${detail.fileName || '파일'}을(를) 완전히 검사하지 못했습니다.`,
        detail: lines.join('\n'),
        buttons,
        defaultId: 0,
        cancelId: 0,
        noLink: true,
      }, signal);
      action = response === 1 ? (hasMandatoryMask ? 'send_masked' : 'send_original') : 'cancel';
    }

    if (action === 'send_original' && !signal.aborted) {
      const confirmed = await this._show({
        type: 'warning',
        title: '원본 전송 확인',
        message: '검출 내용을 마스킹하지 않고 원본을 전송할까요?',
        detail: '개인정보나 악성 지시가 외부 AI 서비스로 전송될 수 있습니다.',
        buttons: ['취소', '원본 전송'],
        defaultId: 0,
        cancelId: 0,
        noLink: true,
      }, signal);
      if (confirmed.response !== 1) action = 'cancel';
    }
    return signal.aborted ? 'cancel' : action;
  }
}

module.exports = { ProxyDecisionController, POLL_MS };
