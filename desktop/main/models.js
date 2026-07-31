'use strict';
/**
 * main/models.js
 * 엔진의 /models/status, /models/fetch REST 를 감싸는 헬퍼. ipc.js(설정 화면에서
 * 사용자가 직접 트리거) 와 main.js(설치 직후 자동 설치 루틴) 양쪽에서 공유해서 쓴다.
 */

async function getStatus(engineManager) {
  const base = engineManager.getStatus().baseUrl;
  if (!base) return { pii: { ready: false }, injection: { ready: false } };
  try {
    const res = await fetch(`${base}/models/status`);
    return await res.json();
  } catch {
    return { pii: { ready: false }, injection: { ready: false } };
  }
}

/**
 * 가중치 다운로드를 시작하고 완료까지 폴링한다. onEvent 는 각 진행 이벤트마다 호출된다
 * (진행률 push 는 호출자가 원하는 채널로 브로드캐스트).
 * @returns {Promise<any>} 완료 시 result 페이로드
 */
async function fetchModels(engineManager, onEvent) {
  const base = engineManager.getStatus().baseUrl;
  if (!base) throw new Error('엔진이 실행 중이 아닙니다');

  const startRes = await fetch(`${base}/models/fetch`, { method: 'POST' });
  if (!startRes.ok) throw new Error(`모델 다운로드 시작 실패 (${startRes.status})`);
  const { jobId } = await startRes.json();

  let after = 0;
  for (;;) {
    const evRes = await fetch(`${base}/jobs/${jobId}/events?after=${after}`);
    if (!evRes.ok) throw new Error(`진행 상태 조회 실패 (${evRes.status})`);
    const payload = await evRes.json();
    for (const ev of payload.events || []) {
      after = Math.max(after, ev.seq || after);
      if (typeof onEvent === 'function') onEvent(ev);
      if (ev.type === 'done') return ev.result;
      if (ev.type === 'error') throw new Error(ev.message || '모델 다운로드 실패');
    }
    if (payload.done) return null;
    await new Promise((r) => setTimeout(r, 500));
  }
}

module.exports = { getStatus, fetchModels };
