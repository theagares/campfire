'use strict';
/**
 * 프록시가 보류한 검사 결과를 네이티브 확인창으로 보여준다.
 * 대시보드가 트레이에 숨겨진 상태에서도 사람이 안전하게 결정할 수 있다.
 */

const POLL_MS = 500;

class ProxyDecisionController {
  constructor({ engineManager, dialog, fetchImpl = globalThis.fetch, pollMs = POLL_MS }) {
    this.engineManager = engineManager;
    this.dialog = dialog;
    this.fetch = fetchImpl;
    this.pollMs = pollMs;
    this.timer = null;
    this.stopped = true;
    this.currentId = null;
  }

  start() {
    if (!this.stopped) return;
    this.stopped = false;
    this._schedule(0);
  }

  stop() {
    this.stopped = true;
    clearTimeout(this.timer);
    this.timer = null;
  }

  _schedule(delay = this.pollMs) {
    clearTimeout(this.timer);
    if (this.stopped) return;
    this.timer = setTimeout(async () => {
      try {
        await this.checkNow();
      } catch (err) {
        if (!this.stopped && process.env.SECUREDOC_PROXY_DECISION_DEBUG) {
          console.warn('[proxy-decision] 조회 실패:', err.message);
        }
      } finally {
        this._schedule();
      }
    }, delay);
  }

  async _json(baseUrl, path, options = {}) {
    const signal = typeof AbortSignal?.timeout === 'function'
      ? AbortSignal.timeout(2000)
      : undefined;
    const res = await this.fetch(`${baseUrl}${path}`, { ...options, signal });
    if (!res.ok) {
      const err = new Error(`엔진 ${path} 응답 ${res.status}`);
      err.status = res.status;
      throw err;
    }
    return res.json();
  }

  async checkNow() {
    if (this.currentId) return false;
    const status = this.engineManager.getStatus();
    if (!status || status.state !== 'running' || !status.baseUrl) return false;

    const listed = await this._json(status.baseUrl, '/decisions');
    const pending = Array.isArray(listed?.pending) ? listed.pending : [];
    const summary = pending[0];
    if (!summary?.id) return false;

    this.currentId = summary.id;
    try {
      await this._review(status.baseUrl, summary);
    } finally {
      this.currentId = null;
    }
    return true;
  }

  async _review(baseUrl, summary) {
    let detail = summary;
    try {
      detail = await this._json(baseUrl, `/decisions/${encodeURIComponent(summary.id)}`);
    } catch (err) {
      if (err.status === 404 || err.status === 409) return;
    }

    const pii = Number(detail.piiCount || 0);
    const injection = Number(detail.injectionCount || 0);
    const forced = Number(detail.forcedMaskCount || 0);
    const findings = pii + injection + forced;
    const result = detail.result || {};
    const complete = detail.scanStatus === 'ok' && !result.truncated;
    const hasMandatoryMask = forced > 0 || (result.forcedMaskItems || []).length > 0;
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
      const { response } = await this.dialog.showMessageBox({
        type: 'warning',
        title: 'Campfire 전송 검토',
        message: `${detail.fileName || '파일'}에서 위험 요소를 찾았습니다.`,
        detail: lines.join('\n'),
        buttons,
        defaultId: 0,
        cancelId: 1,
        noLink: true,
      });
      action = response === 0 ? 'send_masked'
        : (!hasMandatoryMask && response === 2 ? 'send_original' : 'cancel');
    } else {
      const buttons = hasMandatoryMask ? ['취소', '마스킹본 전송'] : ['취소', '원본 전송'];
      const { response } = await this.dialog.showMessageBox({
        type: 'warning',
        title: 'Campfire 검사 미완료',
        message: `${detail.fileName || '파일'}을(를) 완전히 검사하지 못했습니다.`,
        detail: lines.join('\n'),
        buttons,
        defaultId: 0,
        cancelId: 0,
        noLink: true,
      });
      action = response === 1 ? (hasMandatoryMask ? 'send_masked' : 'send_original') : 'cancel';
    }

    if (action === 'send_original') {
      const confirmed = await this.dialog.showMessageBox({
        type: 'warning',
        title: '원본 전송 확인',
        message: '검출 내용을 마스킹하지 않고 원본을 전송할까요?',
        detail: '개인정보나 악성 지시가 외부 AI 서비스로 전송될 수 있습니다.',
        buttons: ['취소', '원본 전송'],
        defaultId: 0,
        cancelId: 0,
        noLink: true,
      });
      if (confirmed.response !== 1) action = 'cancel';
    }

    try {
      await this._json(baseUrl, `/decisions/${encodeURIComponent(summary.id)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action }),
      });
    } catch (err) {
      if (err.status !== 404 && err.status !== 409) throw err;
    }
  }
}

module.exports = { ProxyDecisionController, POLL_MS };
