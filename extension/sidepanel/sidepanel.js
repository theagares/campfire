/**
 * sidepanel.js  ─  HITL 전용 사이드패널 (PLAN §변경1)
 *
 * 흐름:
 *   1. 로드되면 PANEL_READY 로 SW 에 최신 세션 스냅샷을 요청(패널이 열리는 시점과
 *      검사 시작 타이밍이 어긋나도 상태를 복구).
 *   2. SW 의 PANEL_PROGRESS / PANEL_RESULT / PANEL_ERROR 브로드캐스트를 수신해 렌더.
 *   3. 사용자가 항목 토글 후 [전송]/[취소] → PANEL_DECISION 을 SW 로 전송.
 *      (SW 가 원본 탭 content.js 로 중계 → interceptor 가 마스킹본 치환·재전송)
 *
 * 평소엔 닫혀 있고 결과가 돌아올 때만 열린다 — 유휴/기본 화면 없음.
 */

const TYPE_LABELS = {
  PERSON_NAME: '이름', EMAIL: '이메일', PHONE: '전화번호', ADDRESS: '주소',
  ID_NUMBER: '신분증번호', CREDIT_CARD: '카드번호', DATE_OF_BIRTH: '생년월일',
  ORGANIZATION: '조직기밀', BANK_ACCOUNT: '계좌번호', OTHER_PII: '개인정보',
  INSTRUCTION_OVERRIDE: '명령 재정의', ROLE_MANIPULATION: '역할 조작',
  SYSTEM_PROMPT_LEAK: '시스템 프롬프트 유출', JAILBREAK: '탈옥 시도',
  HIDDEN_COMMAND: '숨겨진 명령', DATA_EXFILTRATION: '데이터 유출 시도',
  OTHER_INJECTION: '프롬프트 인젝션',
};
const labelOf = (t) => TYPE_LABELS[t] ?? t;

const $ = (id) => document.getElementById(id);
const el = {
  counts: $('counts'),
  vProgress: $('view-progress'), vResult: $('view-result'), vError: $('view-error'),
  progressTitle: $('progress-title'), progressSub: $('progress-sub'),
  steps: $('steps'), progressWarn: $('progress-warn'),
  docMeta: $('doc-meta'), diff: $('diff'), items: $('items'),
  errTitle: $('err-title'), errMsg: $('err-msg'),
  footer: $('footer'), maskSummary: $('mask-summary'),
  btnCancel: $('btn-cancel'), btnSend: $('btn-send'),
};

let state = {
  sessionId: null,
  kind: null,       // 'file' | 'prompt'
  result: null,
  meta: null,
  segments: [],
  unmasked: new Set(),   // 마스킹 제외(=원본 유지) 항목 인덱스
  decided: false,
};

// ── 세그먼트 빌드 ────────────────────────────────────────────────────────────
function buildSegments(text, piiItems, injectionItems) {
  const all = [
    ...(piiItems || []).map(i => ({ ...i, cat: 'pii' })),
    ...(injectionItems || []).map(i => ({ ...i, cat: 'inj' })),
  ].sort((a, b) => a.start - b.start);

  const segs = [];
  let cursor = 0, idx = 0;
  for (const it of all) {
    if (it.end <= cursor) continue;
    const start = Math.max(it.start, cursor);
    if (start > cursor) segs.push({ type: 'text', text: text.slice(cursor, start) });
    const original = text.slice(start, it.end);
    if (original) segs.push({ type: 'item', idx: idx++, cat: it.cat, dtype: it.type, label: labelOf(it.type), original });
    cursor = it.end;
  }
  if (cursor < text.length) segs.push({ type: 'text', text: text.slice(cursor) });
  return segs;
}

function esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function renderDiff() {
  el.diff.innerHTML = state.segments.map(seg => {
    if (seg.type === 'text') return esc(seg.text);
    const kept = state.unmasked.has(seg.idx);
    const cls = seg.cat === 'inj' ? 'inj' : 'pii';
    return `<span class="mark ${cls}${kept ? ' kept' : ''}" title="${esc(seg.label)}">${esc(seg.original)}</span>`;
  }).join('');
}

function renderItems() {
  const items = state.segments.filter(s => s.type === 'item');
  if (items.length === 0) {
    el.items.innerHTML = '<div class="empty">탐지된 항목이 없습니다. 원본을 그대로 전송할 수 있습니다.</div>';
    return;
  }
  el.items.innerHTML = items.map(seg => {
    const on = !state.unmasked.has(seg.idx);
    const snip = seg.original.length > 40 ? seg.original.slice(0, 40) + '…' : seg.original;
    return `
      <div class="item">
        <span class="cat ${seg.cat}"></span>
        <div class="body">
          <div class="lbl">${esc(seg.label)}</div>
          <div class="snip">${esc(snip)}</div>
        </div>
        <label class="switch">
          <input type="checkbox" data-idx="${seg.idx}" ${on ? 'checked' : ''}>
          <span class="track"><span class="thumb"></span></span>
        </label>
      </div>`;
  }).join('');

  el.items.querySelectorAll('input[type="checkbox"]').forEach(cb => {
    cb.addEventListener('change', () => {
      const idx = parseInt(cb.dataset.idx, 10);
      if (cb.checked) state.unmasked.delete(idx); else state.unmasked.add(idx);
      renderDiff();
      refreshSummary();
    });
  });
}

function refreshCounts() {
  const pii = state.result?.stats?.piiCount ?? 0;
  const inj = state.result?.stats?.injectionCount ?? 0;
  el.counts.textContent = `PII ${pii}건 | INJECTION ${inj}건 탐지`;
  el.counts.classList.toggle('has-risk', pii + inj > 0);
}

function refreshSummary() {
  const total = state.segments.filter(s => s.type === 'item').length;
  const maskCount = total - state.unmasked.size;
  if (maskCount > 0) {
    el.maskSummary.textContent = `${maskCount}건 마스킹 후 전송`;
    el.maskSummary.classList.remove('clear');
  } else {
    el.maskSummary.textContent = total > 0 ? '마스킹 없이 원본 전송' : '안전 — 원본 전송';
    el.maskSummary.classList.add('clear');
  }
}

// ── 뷰 전환 ──────────────────────────────────────────────────────────────────
function showView(name) {
  el.vProgress.hidden = name !== 'progress';
  el.vResult.hidden = name !== 'result';
  el.vError.hidden = name !== 'error';
  el.footer.hidden = name !== 'result';
}

function applyProgress(event) {
  if (!event) return;
  if (event.type === 'warning') {
    el.progressWarn.hidden = false;
    el.progressWarn.textContent = `이 입력은 검사되지 않았습니다 (사유: ${event.reason || event.scanStatus || '알 수 없음'})`;
    return;
  }
  if (event.type !== 'step') return;
  el.steps.querySelectorAll('.step').forEach(li => {
    const s = parseInt(li.dataset.step, 10);
    if (s < event.step) { li.classList.add('done'); li.classList.remove('active'); }
    else if (s === event.step) {
      li.classList.toggle('done', !!event.done);
      li.classList.toggle('active', !event.done);
    }
  });
  if (event.label) el.progressTitle.textContent = event.label;
}

function renderProgress(session) {
  showView('progress');
  el.counts.textContent = '검사 중…';
  el.counts.classList.remove('has-risk');
  if (session?.meta?.fileName) {
    el.progressSub.textContent = session.meta.fileName;
  } else if (session?.meta?.textPreview) {
    el.progressSub.textContent = `"${session.meta.textPreview}"`;
  }
  (session?.progress || []).forEach(applyProgress);
}

function renderResult(kind, result, meta) {
  state.kind = kind;
  state.result = result;
  state.meta = meta;
  state.unmasked = new Set();
  state.decided = false;

  // 미검사 통과(파싱 실패/미지원/타임아웃) — PLAN §9.2
  if (result.scanStatus && result.scanStatus !== 'ok') {
    showView('error');
    el.errTitle.textContent = '검사하지 못했습니다';
    el.errMsg.textContent =
      `사유: ${result.reason || result.scanStatus}\n검사 없이 전송하려면 사이트에서 다시 시도하세요.`;
    return;
  }

  refreshCounts();

  if (meta?.fileName) {
    el.docMeta.innerHTML = `📄 <strong>${esc(meta.fileName)}</strong>`;
  } else if (result.originalLength || result.stats?.originalLength) {
    el.docMeta.innerHTML = `💬 프롬프트 (${result.stats?.originalLength ?? 0}자)`;
  } else {
    el.docMeta.innerHTML = '💬 프롬프트';
  }

  state.segments = buildSegments(result.originalText || '', result.piiItems, result.injectionItems);
  renderDiff();
  renderItems();
  refreshSummary();

  // 인젝션 차단 정책(block)인 경우 전송 비활성
  if (result.blocked) {
    el.btnSend.disabled = true;
    el.maskSummary.textContent = '인젝션 차단 정책 — 전송 불가';
  } else {
    el.btnSend.disabled = false;
  }
  showView('result');
}

function renderError(error, meta) {
  showView('error');
  el.errTitle.textContent = '검사 중 오류가 발생했습니다';
  el.errMsg.textContent = error || '엔진에 연결하지 못했습니다.';
  if (meta?.fileName) el.docMeta.textContent = meta.fileName;
}

// ── 결정 전송 ────────────────────────────────────────────────────────────────
function sendDecision(decision) {
  if (state.decided) return;
  state.decided = true;
  chrome.runtime.sendMessage({ type: 'PANEL_DECISION', sessionId: state.sessionId, decision });
  // 결정 후 패널을 닫는다(다음 검사 때 다시 열림 — 유휴 화면 없음)
  setTimeout(() => window.close(), 150);
}

function buildFinalText() {
  return state.segments.map(seg => {
    if (seg.type === 'text') return seg.text;
    return state.unmasked.has(seg.idx) ? seg.original : `[${seg.label} 마스킹]`;
  }).join('');
}

el.btnCancel.addEventListener('click', () => sendDecision({ action: 'cancel' }));

el.btnSend.addEventListener('click', async () => {
  const total = state.segments.filter(s => s.type === 'item').length;
  const maskCount = total - state.unmasked.size;

  if (state.kind === 'prompt') {
    if (maskCount <= 0) { sendDecision({ action: 'passthrough' }); return; }
    sendDecision({ action: 'masked', maskedText: buildFinalText() });
    return;
  }

  // file
  if (maskCount <= 0) { sendDecision({ action: 'passthrough' }); return; }

  // 토글 변경이 없고 엔진이 만든 완전 마스킹본이 있으면 그대로 사용
  if (state.unmasked.size === 0 && state.result.maskedFile) {
    const mf = state.result.maskedFile;
    sendDecision({ action: 'upload', maskedBase64: mf.base64, mimeType: mf.mimeType, fileName: mf.fileName });
    return;
  }

  // 토글 반영 → SW 에 파일 재생성 요청
  el.btnSend.disabled = true;
  el.btnSend.textContent = '준비 중…';
  const finalText = buildFinalText();
  chrome.runtime.sendMessage(
    { type: 'WRAP_MASKED_TEXT', payload: { text: finalText, mimeType: state.meta?.mimeType || '', fileName: state.meta?.fileName || 'document' } },
    (res) => {
      if (res?.success) {
        sendDecision({ action: 'upload', maskedBase64: res.base64, mimeType: res.mime, fileName: res.name });
      } else {
        sendDecision({ action: 'cancel' });
      }
    },
  );
});

// ── SW 메시지 수신 ───────────────────────────────────────────────────────────
chrome.runtime.onMessage.addListener((msg) => {
  if (!msg || (msg.sessionId && state.sessionId && msg.sessionId !== state.sessionId)) {
    // 다른 세션의 이벤트는 무시 (단, 아직 세션 미확정이면 채택)
    if (msg?.sessionId && !state.sessionId) state.sessionId = msg.sessionId;
    else return;
  }
  if (msg.type === 'PANEL_PROGRESS') {
    state.sessionId = msg.sessionId;
    applyProgress(msg.event);
  } else if (msg.type === 'PANEL_RESULT') {
    state.sessionId = msg.sessionId;
    renderResult(msg.kind, msg.result, msg.meta);
  } else if (msg.type === 'PANEL_ERROR') {
    state.sessionId = msg.sessionId;
    renderError(msg.error, msg.meta);
  }
});

// ── 로드 시 최신 세션 스냅샷 pull ────────────────────────────────────────────
function pullSnapshot() {
  chrome.runtime.sendMessage({ type: 'PANEL_READY' }, (res) => {
    if (chrome.runtime.lastError) return;
    if (!res?.session) { renderProgress(null); return; }
    state.sessionId = res.sessionId;
    const s = res.session;
    if (s.status === 'ready' && s.result) renderResult(s.kind, s.result, s.meta);
    else if (s.status === 'error') renderError(s.error, s.meta);
    else renderProgress(s);
  });
}

renderProgress(null);
pullSnapshot();
