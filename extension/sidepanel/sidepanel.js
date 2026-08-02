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
  progressFill: $('progress-fill'), progressWarn: $('progress-warn'),
  docName: $('doc-name'), docType: $('doc-type'), diff: $('diff'), items: $('items'),
  errTitle: $('err-title'), errMsg: $('err-msg'),
  footer: $('footer'), maskSummary: $('mask-summary'),
  btnCancel: $('btn-cancel'), btnSend: $('btn-send'), btnClose: $('btn-close'),
};

// 진행 단계 순서(2번째 인젝션 탐지가 3이 아닌 4인 것은 SW 쪽 단계 정의를 따름)
const PROGRESS_STEP_ORDER = [1, 2, 4, 5];

let state = {
  sessionId: null,
  myTabId: null,    // 이 패널 인스턴스가 속한 탭 — 다른 탭 대상 브로드캐스트를 걸러내는 데 씀
  kind: null,       // 'file' | 'prompt' | 'combined'
  result: null,
  meta: null,
  segments: [],          // kind: 'file' | 'prompt'
  docSegments: [],        // kind: 'combined' — 문서 쪽
  promptSegments: [],     // kind: 'combined' — 프롬프트 쪽
  unmasked: new Set(),   // 마스킹 제외(=원본 유지) 항목 인덱스(문서/프롬프트 idx 공유 — buildSegments 의 offset 으로 겹치지 않게 함)
  groups: [],            // 탐지 유형(dtype)별 묶음 — renderItems() 가 채운다
  expanded: new Set(),   // 펼쳐진 그룹의 dtype
  decided: false,
};

// chrome.runtime.sendMessage 브로드캐스트는 열려 있는 모든 탭의 패널 인스턴스에
// 전역으로 도달한다 — 내 탭 ID를 최대한 빨리 알아둬야, 아직 세션이 미확정인
// 상태에서 "다른 탭"의 이벤트를 내 것으로 잘못 채택하는 걸 막을 수 있다.
//
// 네이티브 사이드패널로 열렸을 때는 SW 에 물어봐도 답이 없다 — 사이드패널은 탭에
// 종속되지 않은 확장 페이지라 sender.tab 이 비어 있기 때문. 그래서 SW 가 패널을
// 열 때 URL 에 심어준 ?tabId= 를 먼저 읽고, 그게 없을 때(= iframe 오버레이 폴백으로
// 열린 경우, 그쪽은 페이지의 프레임이라 sender.tab 이 있다) SW 에 물어본다.
// 파라미터가 없을 때 Number(null) 은 0 이 되어버린다 — 그걸 탭 ID 로 채택하면 모든
// 브로드캐스트가 "내 탭이 아님"으로 걸러져 패널이 영영 안 그려진다. 원문부터 확인한다.
const rawTabId = new URLSearchParams(location.search).get('tabId');
if (/^\d+$/.test(rawTabId ?? '')) state.myTabId = Number(rawTabId);

if (state.myTabId == null) {
  chrome.runtime.sendMessage({ type: 'GET_TAB_ID' }, (res) => {
    if (chrome.runtime.lastError) return;
    if (res?.tabId != null) state.myTabId = res.tabId;
  });
}

// ── 세그먼트 빌드 ────────────────────────────────────────────────────────────
// idxOffset: combined 모드에서 문서/프롬프트 두 세그먼트 배열의 idx 가 서로 겹치지
// 않게(체크박스 data-idx 유일성, state.unmasked Set 공유) 시작 번호를 밀어준다.
function buildSegments(text, piiItems, injectionItems, idxOffset = 0) {
  const all = [
    ...(piiItems || []).map(i => ({ ...i, cat: 'pii' })),
    ...(injectionItems || []).map(i => ({ ...i, cat: 'inj' })),
  ].sort((a, b) => a.start - b.start);

  const segs = [];
  let cursor = 0, idx = idxOffset;
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

const tipFor = (label, masked) => `${label} — ${masked ? '눌러서 마스킹 해제' : '눌러서 다시 마스킹'}`;

// 마킹된 구간은 그 자체가 버튼이다 — 문서에서 바로 눌러 마스킹을 풀거나 다시 건다.
// (우측 목록의 토글과 같은 상태를 공유하며, 어느 쪽을 바꿔도 양쪽이 함께 갱신된다.)
function segmentsToHtml(segments) {
  return segments.map(seg => {
    if (seg.type === 'text') return esc(seg.text);
    const masked = !state.unmasked.has(seg.idx);
    const cls = seg.cat === 'inj' ? 'inj' : 'pii';
    return `<span class="mark ${cls}${masked ? '' : ' kept'}" data-idx="${seg.idx}" data-label="${esc(seg.label)}"`
      + ` role="button" tabindex="0" aria-pressed="${masked}" title="${esc(tipFor(seg.label, masked))}">${esc(seg.original)}</span>`;
  }).join('');
}

function renderDiff() {
  if (state.kind === 'combined') {
    el.diff.innerHTML = `
      <div class="section-label"><span>📄 문서</span></div>
      <div class="combined-block">${segmentsToHtml(state.docSegments) || '<span class="empty-inline">(빈 문서)</span>'}</div>
      <div class="section-label" style="margin-top:16px"><span>💬 프롬프트</span></div>
      <div class="combined-block">${segmentsToHtml(state.promptSegments) || '<span class="empty-inline">(빈 프롬프트)</span>'}</div>
    `;
    return;
  }
  el.diff.innerHTML = segmentsToHtml(state.segments);
}

function allItemSegments() {
  if (state.kind === 'combined') {
    return [...state.docSegments, ...state.promptSegments].filter(s => s.type === 'item');
  }
  return state.segments.filter(s => s.type === 'item');
}

/** 탐지 항목을 유형(dtype)별로 묶는다. PII 를 먼저, 그 안에서는 건수 많은 순. */
function groupItems() {
  const map = new Map();
  for (const seg of allItemSegments()) {
    let g = map.get(seg.dtype);
    if (!g) {
      g = { dtype: seg.dtype, cat: seg.cat, label: seg.label, segs: [] };
      map.set(seg.dtype, g);
    }
    g.segs.push(seg);
  }
  return [...map.values()].sort((a, b) =>
    a.cat === b.cat ? b.segs.length - a.segs.length : (a.cat === 'pii' ? -1 : 1));
}

const maskedCountOf = (g) => g.segs.filter(s => !state.unmasked.has(s.idx)).length;
const trunc = (s) => (s.length > 40 ? s.slice(0, 40) + '…' : s);

function renderItems() {
  state.groups = groupItems();
  if (state.groups.length === 0) {
    el.items.innerHTML = '<div class="empty">탐지된 항목이 없습니다. 원본을 그대로 전송할 수 있습니다.</div>';
    return;
  }

  el.items.innerHTML = state.groups.map(g => {
    const masked = maskedCountOf(g);
    const open = state.expanded.has(g.dtype);
    return `
      <div class="group${open ? ' open' : ''}" data-type="${esc(g.dtype)}">
        <div class="group-head" role="button" tabindex="0" aria-expanded="${open}">
          <span class="caret" aria-hidden="true"></span>
          <span class="cat ${g.cat}"></span>
          <div class="g-text">
            <div class="g-label"><span class="g-name">${esc(g.label)}</span><span class="g-count">${g.segs.length}</span></div>
            <div class="g-state">${masked}/${g.segs.length} 마스킹</div>
          </div>
          <label class="switch g-switch">
            <input type="checkbox" class="g-toggle" ${masked === g.segs.length ? 'checked' : ''}>
            <span class="track"><span class="thumb"></span></span>
          </label>
        </div>
        <div class="group-body"${open ? '' : ' hidden'}>
          ${g.segs.map(s => `
            <div class="item" data-idx="${s.idx}">
              <div class="snip">${esc(trunc(s.original))}</div>
              <label class="switch">
                <input type="checkbox" class="i-toggle" data-idx="${s.idx}" ${state.unmasked.has(s.idx) ? '' : 'checked'}>
                <span class="track"><span class="thumb"></span></span>
              </label>
            </div>`).join('')}
        </div>
      </div>`;
  }).join('');

  // indeterminate(일부만 마스킹)는 HTML 속성으로 표현할 수 없어 렌더 후 직접 세팅한다.
  state.groups.forEach(g => syncGroupHead(g.dtype));
}

// ── 상태 동기화 ──────────────────────────────────────────────────────────────
// 문서 diff 는 통째로 다시 그리지 않고 해당 구간만 갱신한다 — 긴 문서에서 마킹을
// 누를 때마다 innerHTML 을 재생성하면 스크롤 위치가 튄다.
function groupElOf(dtype) {
  return [...el.items.querySelectorAll('.group')].find(g => g.dataset.type === dtype) || null;
}

function syncMark(idx) {
  const masked = !state.unmasked.has(idx);
  el.diff.querySelectorAll(`.mark[data-idx="${idx}"]`).forEach(m => {
    m.classList.toggle('kept', !masked);
    m.setAttribute('aria-pressed', String(masked));
    m.title = tipFor(m.dataset.label || '', masked);
  });
}

function syncGroupHead(dtype) {
  const g = state.groups.find(x => x.dtype === dtype);
  const box = groupElOf(dtype);
  if (!g || !box) return;
  const masked = maskedCountOf(g);
  const cb = box.querySelector('.g-toggle');
  cb.checked = masked === g.segs.length;
  cb.indeterminate = masked > 0 && masked < g.segs.length;
  box.querySelector('.g-state').textContent = `${masked}/${g.segs.length} 마스킹`;
}

function syncItemRow(idx) {
  const cb = el.items.querySelector(`.i-toggle[data-idx="${idx}"]`);
  if (cb) cb.checked = !state.unmasked.has(idx);
}

function groupOfIdx(idx) {
  return state.groups.find(g => g.segs.some(s => s.idx === idx)) || null;
}

/** 항목 하나의 마스킹 여부를 바꾸고, 문서·목록·요약을 모두 맞춘다. */
function setMasked(idx, masked) {
  if (masked) state.unmasked.delete(idx); else state.unmasked.add(idx);
  syncMark(idx);
  syncItemRow(idx);
  const g = groupOfIdx(idx);
  if (g) syncGroupHead(g.dtype);
  refreshSummary();
}

/** 그룹 전체를 한 번에 마스킹/해제 — 종류별 일괄 처리가 이 화면의 기본 조작이다. */
function setGroupMasked(dtype, masked) {
  const g = state.groups.find(x => x.dtype === dtype);
  if (!g) return;
  for (const s of g.segs) {
    if (masked) state.unmasked.delete(s.idx); else state.unmasked.add(s.idx);
    syncMark(s.idx);
    syncItemRow(s.idx);
  }
  syncGroupHead(dtype);
  refreshSummary();
}

function toggleGroupOpen(dtype) {
  const box = groupElOf(dtype);
  if (!box) return;
  const open = !state.expanded.has(dtype);
  if (open) state.expanded.add(dtype); else state.expanded.delete(dtype);
  box.classList.toggle('open', open);
  box.querySelector('.group-head').setAttribute('aria-expanded', String(open));
  box.querySelector('.group-body').hidden = !open;
  // 목록 영역이 좁아(사이드패널 기본 폭에서는 화면의 절반 이하) 펼친 내용이 그대로
  // 잘리는 경우가 많다 — 방금 편 그룹은 보이는 곳까지 끌어온다.
  if (open) box.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}

// ── 이벤트(위임) ─────────────────────────────────────────────────────────────
// 렌더가 innerHTML 을 갈아끼워도 살아남도록 컨테이너에 한 번만 건다.

// 문서 본문: 마킹된 구간 클릭 → 마스킹 해제/재마스킹
el.diff.addEventListener('click', (e) => {
  const mark = e.target.closest('.mark');
  if (!mark) return;
  const idx = Number(mark.dataset.idx);
  if (Number.isFinite(idx)) setMasked(idx, state.unmasked.has(idx));
});
el.diff.addEventListener('keydown', (e) => {
  if (e.key !== 'Enter' && e.key !== ' ') return;
  const mark = e.target.closest('.mark');
  if (!mark) return;
  e.preventDefault(); // 스페이스로 스크롤되는 것 방지
  const idx = Number(mark.dataset.idx);
  if (Number.isFinite(idx)) setMasked(idx, state.unmasked.has(idx));
});

// 우측 목록: 그룹 토글 / 개별 토글
el.items.addEventListener('change', (e) => {
  const t = e.target;
  if (t.classList.contains('g-toggle')) {
    const box = t.closest('.group');
    if (box) setGroupMasked(box.dataset.type, t.checked);
  } else if (t.classList.contains('i-toggle')) {
    const idx = Number(t.dataset.idx);
    if (Number.isFinite(idx)) setMasked(idx, t.checked);
  }
});

// 우측 목록: 헤더 클릭 → 펼치기/접기 (토글 스위치를 누른 경우는 제외)
el.items.addEventListener('click', (e) => {
  if (e.target.closest('.switch')) return;
  const head = e.target.closest('.group-head');
  if (!head) return;
  const box = head.closest('.group');
  if (box) toggleGroupOpen(box.dataset.type);
});
el.items.addEventListener('keydown', (e) => {
  if (e.key !== 'Enter' && e.key !== ' ') return;
  if (e.target.closest('.switch')) return;
  const head = e.target.closest('.group-head');
  if (!head) return;
  e.preventDefault();
  const box = head.closest('.group');
  if (box) toggleGroupOpen(box.dataset.type);
});

function refreshCounts() {
  const pii = state.result?.stats?.piiCount ?? 0;
  const inj = state.result?.stats?.injectionCount ?? 0;
  el.counts.textContent = `PII ${pii}건 | INJECTION ${inj}건 탐지`;
}

function refreshSummary() {
  const total = allItemSegments().length;
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
  const order = PROGRESS_STEP_ORDER.indexOf(event.step);
  if (order >= 0) {
    const pct = ((order + (event.done ? 1 : 0.5)) / PROGRESS_STEP_ORDER.length) * 100;
    el.progressFill.style.width = `${Math.max(8, Math.min(100, pct))}%`;
  }
  if (event.label) el.progressTitle.textContent = event.label;
}

function renderProgress(session) {
  showView('progress');
  el.counts.textContent = '검사 중…';
  el.progressFill.style.width = '8%';
  if (session?.meta?.fileName) {
    el.docName.textContent = session.meta.fileName;
    el.docType.textContent = '문서 검사 중';
    el.progressSub.textContent = session.meta.fileName;
  } else if (session?.meta?.textPreview) {
    el.docName.textContent = 'Campfire';
    el.docType.textContent = '프롬프트 검사 중';
    el.progressSub.textContent = `"${session.meta.textPreview}"`;
  } else {
    el.docName.textContent = 'Campfire';
    el.docType.textContent = '문서 검토';
  }
  (session?.progress || []).forEach(applyProgress);
}

function renderResult(kind, result, meta) {
  state.kind = kind;
  state.result = result;
  state.meta = meta;
  state.unmasked = new Set();
  state.groups = [];
  state.expanded = new Set();
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

  if (kind === 'combined') {
    el.docName.textContent = meta?.fileName || 'Campfire';
    el.docType.textContent = '문서 + 프롬프트 검토';
    state.docSegments = buildSegments(result.originalText || '', result.piiItems, result.injectionItems, 0);
    state.promptSegments = buildSegments(
      result.userPromptOriginal || '', result.userPromptPiiItems, [], state.docSegments.length,
    );
  } else if (meta?.fileName) {
    el.docName.textContent = meta.fileName;
    el.docType.textContent = meta.mimeType?.includes('pdf') ? 'PDF · 문서 검토' : '문서 검토';
    state.segments = buildSegments(result.originalText || '', result.piiItems, result.injectionItems);
  } else if (result.originalLength || result.stats?.originalLength) {
    el.docName.textContent = 'Campfire';
    el.docType.textContent = `프롬프트 (${result.stats?.originalLength ?? 0}자)`;
    state.segments = buildSegments(result.originalText || '', result.piiItems, result.injectionItems);
  } else {
    el.docName.textContent = 'Campfire';
    el.docType.textContent = '프롬프트 검토';
    state.segments = buildSegments(result.originalText || '', result.piiItems, result.injectionItems);
  }

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
  if (meta?.fileName) { el.docName.textContent = meta.fileName; el.docType.textContent = '오류'; }
}

// ── 결정 전송 ────────────────────────────────────────────────────────────────
function sendDecision(decision) {
  if (state.decided) return;
  state.decided = true;
  chrome.runtime.sendMessage({ type: 'PANEL_DECISION', sessionId: state.sessionId, decision });
  // 결정 후 패널을 닫는다(다음 검사 때 다시 열림 — 유휴 화면 없음).
  // 두 호스팅 방식을 한 번에 커버한다: 네이티브 사이드패널은 window.close() 로 닫히고
  // (그때 window.parent 는 자기 자신이라 postMessage 는 아무도 안 받는다), iframe
  // 오버레이 폴백으로 열렸을 때는 window.close() 가 무효인 대신 content.js 가 이
  // postMessage 를 받아 DOM 에서 제거한다.
  setTimeout(() => {
    try { window.parent.postMessage({ type: 'UPS_CLOSE_OVERLAY' }, '*'); } catch (_) {}
    window.close();
  }, 150);
}

function buildFinalTextFrom(segments) {
  return segments.map(seg => {
    if (seg.type === 'text') return seg.text;
    return state.unmasked.has(seg.idx) ? seg.original : `[${seg.label} 마스킹]`;
  }).join('');
}

function buildFinalText() {
  return buildFinalTextFrom(state.segments);
}

el.btnCancel.addEventListener('click', () => sendDecision({ action: 'cancel' }));
el.btnClose.addEventListener('click', () => sendDecision({ action: 'cancel' }));

function wrapMaskedFileAsync(text, mimeType, fileName) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage(
      { type: 'WRAP_MASKED_TEXT', payload: { text, mimeType: mimeType || '', fileName: fileName || 'document' } },
      (res) => resolve(res?.success ? res : null),
    );
  });
}

el.btnSend.addEventListener('click', async () => {
  if (state.kind === 'combined') {
    const docItems = state.docSegments.filter(s => s.type === 'item');
    const docUnmaskedCount = docItems.filter(s => state.unmasked.has(s.idx)).length;
    const finalPromptText = buildFinalTextFrom(state.promptSegments);

    let file;
    if (docItems.length === 0) {
      file = { action: 'passthrough' };
    } else if (docUnmaskedCount === 0 && state.result.maskedFile) {
      // 문서 쪽 토글 변경이 전혀 없고(모두 마스킹 유지) 엔진이 만든 완전 마스킹본이 있으면 그대로 사용
      const mf = state.result.maskedFile;
      file = { action: 'upload', maskedBase64: mf.base64, mimeType: mf.mimeType, fileName: mf.fileName };
    } else if (docUnmaskedCount === docItems.length) {
      // 문서 쪽 항목을 전부 마스킹 해제(원본 그대로) 했으면 파일도 원본 그대로 전달
      file = { action: 'passthrough' };
    } else {
      el.btnSend.disabled = true;
      el.btnSend.textContent = '준비 중…';
      const finalDocText = buildFinalTextFrom(state.docSegments);
      const wrapped = await wrapMaskedFileAsync(finalDocText, state.meta?.mimeType, state.meta?.fileName);
      file = wrapped
        ? { action: 'upload', maskedBase64: wrapped.base64, mimeType: wrapped.mime, fileName: wrapped.name }
        : { action: 'cancel' };
    }

    sendDecision({ action: 'send', maskedText: finalPromptText, file });
    return;
  }

  const total = allItemSegments().length;
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
  const wrapped = await wrapMaskedFileAsync(finalText, state.meta?.mimeType, state.meta?.fileName);
  if (wrapped) {
    sendDecision({ action: 'upload', maskedBase64: wrapped.base64, mimeType: wrapped.mime, fileName: wrapped.name });
  } else {
    sendDecision({ action: 'cancel' });
  }
});

// ── SW 메시지 수신 ───────────────────────────────────────────────────────────
chrome.runtime.onMessage.addListener((msg) => {
  if (!msg) return;
  // 최우선 필터: 다른 탭 대상 브로드캐스트는 세션 확정 여부와 무관하게 무조건 무시.
  // (이게 없으면, 아직 세션이 없는 상태의 패널이 다른 탭의 이벤트를 "내 것"으로
  // 잘못 채택해버린다 — 탭 스코핑이 안 먹히는 것처럼 보인 실제 원인 중 하나.)
  if (msg.tabId != null && state.myTabId != null && msg.tabId !== state.myTabId) return;
  // 이미 다른 세션을 추적 중인데 이번 이벤트가 그 세션이 아니면 무시.
  if (msg.sessionId && state.sessionId && msg.sessionId !== state.sessionId) return;
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
  // tabId 를 같이 보낸다 — SW 는 "요청한 탭의 세션"만 돌려주는데, 네이티브 패널은
  // sender.tab 이 없어 이걸 안 보내면 그 스코핑이 통째로 무너진다(과거에 탭 A 의
  // 결과가 탭 B 패널에 뜨던 버그의 방어선).
  chrome.runtime.sendMessage({ type: 'PANEL_READY', tabId: state.myTabId }, (res) => {
    if (chrome.runtime.lastError) return;
    if (res?.tabId != null) state.myTabId = res.tabId;
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
