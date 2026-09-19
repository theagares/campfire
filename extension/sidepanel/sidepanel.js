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
 *
 * 세그먼트 분할과 최종 문자열 생성은 utils/mask-segments.js 로 옮겼다. 여기서 만든
 * 미리보기와 실제로 업로드되는 파일이 **다른 코드**로 만들어지면 언제든 갈라지기
 * 때문이다(다중 첨부에서는 최종본을 SW 가 만든다). 그래서 이 파일은 module 로 돈다
 * — sidepanel.html 의 script 태그에 type="module" 이 필요하다.
 */

import { buildSegments, buildFinalText, labelOf } from '../utils/mask-segments.js';

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
  tabs: $('tabs'),
};

// 진행 단계 순서(2번째 인젝션 탐지가 3이 아닌 4인 것은 SW 쪽 단계 정의를 따름)
const PROGRESS_STEP_ORDER = [1, 2, 4, 5];

let state = {
  sessionId: null,
  seq: null,        // 지금 그리고 있는 세션의 시작 순번(SW 가 매긴다) — 아래 리스너 주석 참고
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
  stale: false,   // 결정이 만료돼 적용되지 못한 상태 — 아래 renderStaleDecision 참고

  // ── 다중 첨부 ──
  // docs 는 SW 가 보내온 **메타**만 담는다. 본문(originalText/탐지 항목)은 활성 탭을
  // 그릴 때만 따로 끌어온다 — 브로드캐스트는 열려 있는 모든 확장 페이지에 전역으로
  // 도달하므로 거기에 원문을 실으면 안 된다.
  docs: [],
  promptMeta: null,
  activeTab: null,      // docId 또는 'prompt'
  loaded: new Map(),    // itemId -> { originalText, piiItems, injectionItems }
  decisions: new Map(), // docId -> 'masked'|'original'|'exclude'
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

function esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

const tipFor = (label, masked) => `${label} — ${masked ? '눌러서 마스킹 해제' : '눌러서 다시 마스킹'}`;

// 마킹된 구간은 그 자체가 버튼이다 — 문서에서 바로 눌러 마스킹을 풀거나 다시 건다.
// (우측 목록의 토글과 같은 상태를 공유하며, 어느 쪽을 바꿔도 양쪽이 함께 갱신된다.)
function segmentsToHtml(segments) {
  return segments.map(seg => {
    if (seg.type === 'text') return esc(seg.text);
    const masked = !state.unmasked.has(seg.key);
    const cls = seg.cat === 'inj' ? 'inj' : 'pii';
    return `<span class="mark ${cls}${masked ? '' : ' kept'}" data-key="${esc(seg.key)}" data-label="${esc(seg.label)}"`
      + ` role="button" tabindex="0" aria-pressed="${masked}" title="${esc(tipFor(seg.label, masked))}">${esc(seg.original)}</span>`;
  }).join('');
}

// ── 다중 첨부: 탭 ───────────────────────────────────────────────────────────
const STATUS_LABEL = {
  pending: '대기 중…', scanning: '검사 중…', error: '실패',
  truncated: '부분 검사', unsupported: '미지원', excluded: '제외됨',
};

function tabLabel(doc) {
  if (doc.status === 'done') {
    const n = (doc.counts?.pii || 0) + (doc.counts?.injection || 0);
    return `${doc.fileName} · ${n}건`;
  }
  return `${doc.fileName} · ${STATUS_LABEL[doc.status] || doc.status}`;
}

function renderTabs() {
  if (state.kind !== 'multi') { el.tabs.hidden = true; return; }
  el.tabs.hidden = false;

  const items = [
    ...state.docs.map(d => ({ id: d.id, label: tabLabel(d), status: d.status })),
    { id: 'prompt', label: `프롬프트 · ${promptCountLabel()}`, status: state.promptMeta?.status || 'pending' },
  ];

  el.tabs.innerHTML = items.map(it => {
    const on = it.id === state.activeTab;
    return `<button role="tab" data-item="${esc(it.id)}" class="st-${esc(it.status)}"`
      + ` aria-selected="${on}" tabindex="${on ? 0 : -1}">${esc(it.label)}</button>`;
  }).join('');
}

function promptCountLabel() {
  const p = state.promptMeta;
  if (!p || p.status === 'pending') return '대기 중…';
  if (p.status === 'error') return '실패';
  return `${(p.counts?.pii || 0) + (p.counts?.injection || 0)}건`;
}

/** 활성 탭의 본문을 SW 에서 끌어온다. 이미 받은 항목은 다시 요청하지 않는다. */
function pullItem(itemId) {
  if (state.loaded.has(itemId)) return Promise.resolve(state.loaded.get(itemId));
  return new Promise((resolve) => {
    chrome.runtime.sendMessage({
      type: 'GET_PANEL_ITEM_RESULT', sessionId: state.sessionId, tabId: state.myTabId, itemId,
    }, (res) => {
      void chrome.runtime.lastError;
      if (!res?.ok) { resolve(null); return; }
      const item = {
        originalText: res.originalText || '',
        piiItems: res.piiItems || [],
        injectionItems: res.injectionItems || [],
      };
      state.loaded.set(itemId, item);
      resolve(item);
    });
  });
}

async function showTab(itemId) {
  state.activeTab = itemId;
  renderTabs();

  const doc = state.docs.find(d => d.id === itemId);
  if (doc && doc.status !== 'done' && doc.status !== 'truncated') {
    // 아직 결과가 없는 탭 — 상태만 알리고 본문은 비워 둔다. 조용히 빈 화면을
    // 보여주면 "검사했는데 탐지가 없다" 로 읽힌다.
    el.diff.innerHTML = `<div class="empty">${esc(doc.error || STATUS_LABEL[doc.status] || '')}</div>`;
    el.items.innerHTML = '';
    refreshSummary();
    return;
  }

  const item = await pullItem(itemId);
  if (state.activeTab !== itemId) return;   // 그 사이 사용자가 다른 탭으로 갔다
  if (!item) { el.diff.innerHTML = '<div class="empty">결과를 불러오지 못했습니다</div>'; return; }

  state.segments = buildSegments(item.originalText, item.piiItems, item.injectionItems, itemId);
  renderDiff();
  renderItems();
  refreshSummary();
}

el.tabs.addEventListener('click', (e) => {
  const btn = e.target.closest('button[data-item]');
  if (btn) showTab(btn.dataset.item);
});
el.tabs.addEventListener('keydown', (e) => {
  if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
  const btns = [...el.tabs.querySelectorAll('button[data-item]')];
  const at = btns.findIndex(b => b.dataset.item === state.activeTab);
  if (at < 0) return;
  e.preventDefault();
  const next = btns[(at + (e.key === 'ArrowRight' ? 1 : btns.length - 1)) % btns.length];
  next.focus();
  showTab(next.dataset.item);
});

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

/** PII 만 유형(dtype)별로 묶는다. 건수 많은 순.
 *
 *  인젝션은 묶지 않는다 — 같은 유형이라도 문구 하나하나가 서로 다른 공격이라,
 *  "명령 재정의 2건" 으로 접어버리면 정작 읽고 판단해야 할 내용이 가려진다.
 *  이름·이메일처럼 종류만 알면 되는 PII 와 성격이 다르다. */
function groupItems() {
  const map = new Map();
  for (const seg of allItemSegments()) {
    if (seg.cat !== 'pii') continue;
    let g = map.get(seg.dtype);
    if (!g) {
      g = { dtype: seg.dtype, cat: seg.cat, label: seg.label, segs: [] };
      map.set(seg.dtype, g);
    }
    g.segs.push(seg);
  }
  return [...map.values()].sort((a, b) => b.segs.length - a.segs.length);
}

/** 묶지 않고 하나씩 보여줄 항목(= 인젝션). 문서에 나온 순서 그대로. */
const soloItems = () => allItemSegments().filter(s => s.cat !== 'pii');

const maskedCountOf = (g) => g.segs.filter(s => !state.unmasked.has(s.key)).length;
const trunc = (s, n = 40) => (s.length > n ? s.slice(0, n) + '…' : s);

function renderItems() {
  state.groups = groupItems();
  const solos = soloItems();
  if (state.groups.length === 0 && solos.length === 0) {
    el.items.innerHTML = '<div class="empty">탐지된 항목이 없습니다. 원본을 그대로 전송할 수 있습니다.</div>';
    return;
  }

  const groupsHtml = state.groups.map(g => {
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
            <div class="item" data-key="${esc(s.key)}">
              <div class="snip">${esc(trunc(s.original))}</div>
              <label class="switch">
                <input type="checkbox" class="i-toggle" data-key="${esc(s.key)}" ${state.unmasked.has(s.key) ? '' : 'checked'}>
                <span class="track"><span class="thumb"></span></span>
              </label>
            </div>`).join('')}
        </div>
      </div>`;
  }).join('');

  // 인젝션은 접지 않고 한 줄씩 — 유형명과 함께 실제 문구를 바로 보여준다.
  const solosHtml = solos.map(s => `
    <div class="solo" data-key="${esc(s.key)}">
      <span class="cat ${s.cat}"></span>
      <div class="s-text">
        <div class="s-label">${esc(s.label)}</div>
        <div class="s-snip">${esc(trunc(s.original, 90))}</div>
      </div>
      <label class="switch">
        <input type="checkbox" class="i-toggle" data-key="${esc(s.key)}" ${state.unmasked.has(s.key) ? '' : 'checked'}>
        <span class="track"><span class="thumb"></span></span>
      </label>
    </div>`).join('');

  el.items.innerHTML = groupsHtml + solosHtml;

  // indeterminate(일부만 마스킹)는 HTML 속성으로 표현할 수 없어 렌더 후 직접 세팅한다.
  state.groups.forEach(g => syncGroupHead(g.dtype));
}

// ── 상태 동기화 ──────────────────────────────────────────────────────────────
// 문서 diff 는 통째로 다시 그리지 않고 해당 구간만 갱신한다 — 긴 문서에서 마킹을
// 누를 때마다 innerHTML 을 재생성하면 스크롤 위치가 튄다.
function groupElOf(dtype) {
  return [...el.items.querySelectorAll('.group')].find(g => g.dataset.type === dtype) || null;
}

function syncMark(key) {
  const masked = !state.unmasked.has(key);
  el.diff.querySelectorAll(`.mark[data-key="${CSS.escape(key)}"]`).forEach(m => {
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

function syncItemRow(key) {
  const cb = el.items.querySelector(`.i-toggle[data-key="${CSS.escape(key)}"]`);
  if (cb) cb.checked = !state.unmasked.has(key);
}

function groupOfKey(key) {
  return state.groups.find(g => g.segs.some(s => s.key === key)) || null;
}

/** 항목 하나의 마스킹 여부를 바꾸고, 문서·목록·요약을 모두 맞춘다. */
function setMasked(key, masked) {
  if (masked) state.unmasked.delete(key); else state.unmasked.add(key);
  syncMark(key);
  syncItemRow(key);
  const g = groupOfKey(key);
  if (g) syncGroupHead(g.dtype);
  refreshSummary();
}

/** 그룹 전체를 한 번에 마스킹/해제 — 종류별 일괄 처리가 이 화면의 기본 조작이다. */
function setGroupMasked(dtype, masked) {
  const g = state.groups.find(x => x.dtype === dtype);
  if (!g) return;
  for (const s of g.segs) {
    if (masked) state.unmasked.delete(s.key); else state.unmasked.add(s.key);
    syncMark(s.key);
    syncItemRow(s.key);
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
  const key = mark.dataset.key;
  if (key) setMasked(key, state.unmasked.has(key));
});
el.diff.addEventListener('keydown', (e) => {
  if (e.key !== 'Enter' && e.key !== ' ') return;
  const mark = e.target.closest('.mark');
  if (!mark) return;
  e.preventDefault(); // 스페이스로 스크롤되는 것 방지
  const key = mark.dataset.key;
  if (key) setMasked(key, state.unmasked.has(key));
});

// 우측 목록: 그룹 토글 / 개별 토글
el.items.addEventListener('change', (e) => {
  const t = e.target;
  if (t.classList.contains('g-toggle')) {
    const box = t.closest('.group');
    if (box) setGroupMasked(box.dataset.type, t.checked);
  } else if (t.classList.contains('i-toggle')) {
    const key = t.dataset.key;
    if (key) setMasked(key, t.checked);
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
    // partial 은 "검사는 했는데 일부만" 이다(문서가 길어 앞부분만 본 경우). 파싱 실패로
    // 아예 검사를 못 한 것과 상황이 다른데 같은 문구를 쓰면 거짓말이 된다 — 나눈다.
    el.progressWarn.textContent = event.partial
      ? (event.reason || '문서의 일부만 검사했습니다')
      : `이 입력은 검사되지 않았습니다 (사유: ${event.reason || event.scanStatus || '알 수 없음'})`;
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
    // 두 배열이 같은 state.unmasked Set 을 공유하므로 키가 겹치면 안 된다.
    // 예전엔 문서 세그먼트 길이를 숫자 offset 으로 밀어 겹침을 피했는데, 그러면
    // 문서 쪽 항목 수가 바뀔 때마다 프롬프트 항목 번호가 통째로 밀린다.
    // 접두사로 나누면 서로의 변화에 영향을 받지 않는다.
    state.docSegments = buildSegments(result.originalText || '', result.piiItems, result.injectionItems, 'doc');
    state.promptSegments = buildSegments(
      result.userPromptOriginal || '', result.userPromptPiiItems, [], 'prompt',
    );
  } else if (meta?.fileName) {
    el.docName.textContent = meta.fileName;
    el.docType.textContent = meta.mimeType?.includes('pdf') ? 'PDF · 문서 검토' : '문서 검토';
    state.segments = buildSegments(result.originalText || '', result.piiItems, result.injectionItems, 'doc');
  } else if (result.originalLength || result.stats?.originalLength) {
    el.docName.textContent = 'Campfire';
    el.docType.textContent = `프롬프트 (${result.stats?.originalLength ?? 0}자)`;
    state.segments = buildSegments(result.originalText || '', result.piiItems, result.injectionItems, 'doc');
  } else {
    el.docName.textContent = 'Campfire';
    el.docType.textContent = '프롬프트 검토';
    state.segments = buildSegments(result.originalText || '', result.piiItems, result.injectionItems, 'doc');
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
/** 결정 후 패널을 닫는다(다음 검사 때 다시 열림 — 유휴 화면 없음).
 *  검토 UI 는 네이티브 사이드패널 하나뿐이므로 window.close() 면 충분하다
 *  (iframe 오버레이 폴백과 그쪽을 닫던 UPS_CLOSE_OVERLAY 는 걷어냈다). */
function closeSelf() {
  setTimeout(() => { window.close(); }, 150);
}

/** 결정이 아무 데도 전달되지 못했음을 화면에 남긴다.
 *
 *  조용히 닫으면 성공과 구분이 안 된다 — 2026-09-06 실사용 재현에서 사용자는
 *  [전송]을 눌렀고 패널은 닫혔는데 사이트로는 아무것도 나가지 않았다. 눌린 줄
 *  알고 기다리게 되는 이 침묵이 이 버그의 실제 피해였다. */
function renderStaleDecision() {
  state.stale = true;
  showView('error');
  el.errTitle.textContent = '이 검토는 만료되었습니다';
  el.errMsg.textContent = '검토 창이 오래되어 결정을 적용하지 못했습니다. 입력창에서 다시 보내주세요.';
}

function sendDecision(decision) {
  // 만료 안내가 떠 있는 상태에서 [취소]/[닫기]를 누르면 닫히기만 하면 된다.
  if (state.stale) { closeSelf(); return; }
  if (state.decided) return;
  state.decided = true;
  // 응답을 반드시 본다. SW 가 모르는 sessionId 였다면 이 결정은 어디에도 전달되지
  // 않는데(service-worker.js 의 PANEL_DECISION 주석 참고), 예전엔 응답을 보지 않고
  // 그대로 닫아서 성공한 것처럼 보였다.
  chrome.runtime.sendMessage(
    { type: 'PANEL_DECISION', sessionId: state.sessionId, tabId: state.myTabId, decision },
    (res) => {
      void chrome.runtime.lastError;
      if (res && res.ok === false) { renderStaleDecision(); return; }
      closeSelf();
    },
  );
}

function sendMultiDecision(decision) {
  if (state.stale) { closeSelf(); return; }
  if (state.decided) return;
  state.decided = true;
  chrome.runtime.sendMessage(
    { type: 'PANEL_MULTI_DECISION', sessionId: state.sessionId, tabId: state.myTabId, decision },
    (res) => {
      void chrome.runtime.lastError;
      if (res && res.ok === false) { renderStaleDecision(); return; }
      closeSelf();
    },
  );
}

/** 해제 키를 소유자별로 나눈다. 키가 `itemId:n` 이라 접두사로 가른다. */
function unmaskedKeysFor(itemId) {
  return [...state.unmasked].filter(k => k.slice(0, k.lastIndexOf(':')) === itemId);
}

/** 다중 결정 — 바이너리도 artifactId 도 패널이 만들지 않는다. SW 가 만든다. */
function buildMultiDecision() {
  const files = state.docs.map((doc) => {
    const chosen = state.decisions.get(doc.id);
    if (chosen) return { id: doc.id, action: chosen, unmaskedKeys: unmaskedKeysFor(doc.id) };
    // 검사에 실패했거나 미지원인데 사용자가 아무것도 고르지 않았으면 제외한다 —
    // 조용히 원본으로 통과시키지 않는다.
    if (doc.status !== 'done' && doc.status !== 'truncated') return { id: doc.id, action: 'exclude' };
    return { id: doc.id, action: 'masked', unmaskedKeys: unmaskedKeysFor(doc.id) };
  });
  return {
    action: 'send',
    prompt: { action: 'masked', unmaskedKeys: unmaskedKeysFor('prompt') },
    files,
  };
}

/** 아직 결정이 필요한 항목이 남아 있으면 그 이유를 돌려준다(없으면 null). */
function blockingReason() {
  for (const doc of state.docs) {
    if (doc.status === 'pending' || doc.status === 'scanning') return '검사가 끝나지 않았습니다';
    if (state.decisions.has(doc.id)) continue;
    if (doc.status === 'error') return `${doc.fileName}: 검사 실패 — 제거하거나 다시 시도해 주세요`;
    if (doc.status === 'unsupported') return `${doc.fileName}: 미지원 형식 — 포함할지 골라 주세요`;
    if (doc.status === 'truncated') return `${doc.fileName}: 일부만 검사됨 — 어떻게 보낼지 골라 주세요`;
  }
  if (state.promptMeta?.status === 'error') return '프롬프트 검사에 실패했습니다';
  return null;
}

el.btnCancel.addEventListener('click', () => (
  state.kind === 'multi' ? sendMultiDecision({ action: 'cancel' }) : sendDecision({ action: 'cancel' })
));
el.btnClose.addEventListener('click', () => (
  state.kind === 'multi' ? sendMultiDecision({ action: 'cancel' }) : sendDecision({ action: 'cancel' })
));

function wrapMaskedFileAsync(text, mimeType, fileName) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage(
      { type: 'WRAP_MASKED_TEXT', payload: { text, mimeType: mimeType || '', fileName: fileName || 'document' } },
      (res) => resolve(res?.success ? res : null),
    );
  });
}

el.btnSend.addEventListener('click', async () => {
  // 다중 경로: 전부 끝나고 모든 오류 항목에 결정이 있어야만 나간다.
  if (state.kind === 'multi') {
    const blocked = blockingReason();
    if (blocked) { el.maskSummary.textContent = blocked; return; }
    sendMultiDecision(buildMultiDecision());
    return;
  }

  if (state.kind === 'combined') {
    const docItems = state.docSegments.filter(s => s.type === 'item');
    const docUnmaskedCount = docItems.filter(s => state.unmasked.has(s.key)).length;
    const finalPromptText = buildFinalText(state.promptSegments, state.unmasked);

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
      const finalDocText = buildFinalText(state.docSegments, state.unmasked);
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
    sendDecision({ action: 'masked', maskedText: buildFinalText(state.segments, state.unmasked) });
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
  const finalText = buildFinalText(state.segments, state.unmasked);
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
  // 같은 탭 안에서는 **나중에 시작된 세션이 이긴다.**
  //
  // 예전에는 "추적 중인 세션이 아니면 무조건 무시" 였다. 그런데 패널은 로드될 때 한 번만
  // 세션을 받아오고(pullSnapshot) 그 뒤엔 이 브로드캐스트로만 갱신된다. 그래서 사용자가
  // 검토를 결정 없이 두고 다음 작업을 하면, 패널이 옛 세션 id 를 계속 들고 있어서 새
  // 스캔 결과를 전부 버렸다 — 실측: 파일 스캔이 정상적으로 끝나 job 까지 기록됐는데
  // 화면엔 이전 검토가 그대로 남아 있었다. 사용자에겐 "검사가 안 된다" 로 보이지만
  // 실제로는 검사는 됐고 화면만 막힌 것이라, 증상과 원인이 어긋나 찾기 어려운 종류다.
  //
  // seq 로 순서를 보는 이유: sessionId 는 UUID 라 어느 쪽이 새것인지 알 수 없다. 순번을
  // 쓰면 새 세션을 받아들이면서도, 늦게 도착한 옛 세션 메시지가 새 결과를 덮는 것은
  // 그대로 막을 수 있다(둘 다 필요하다).
  if (msg.seq != null && state.seq != null) {
    if (msg.seq < state.seq) return;                     // 지나간 세션의 뒤늦은 메시지
  } else if (msg.sessionId && state.sessionId && msg.sessionId !== state.sessionId) {
    return;                                              // seq 가 없는 옛 SW 와의 호환 경로
  }
  // ── 다중 첨부 ──
  // 여기로 오는 건 전부 메타다. 본문은 활성 탭이 GET_PANEL_ITEM_RESULT 로 끌어온다.
  if (msg.type === 'PANEL_SCAN_INIT') {
    state.sessionId = msg.sessionId;
    state.seq = msg.seq ?? state.seq;
    state.kind = 'multi';
    state.docs = msg.docs || [];
    state.promptMeta = { status: 'pending', counts: null };
    state.unmasked = new Set();
    state.loaded = new Map();
    state.decisions = new Map();
    state.decided = false;
    // 탭은 스테이징 순서대로 **즉시** 만든다. 검사가 끝난 순서로 생기면 사용자가
    // 방금 붙인 파일이 어디 있는지 못 찾는다.
    state.activeTab = state.docs[0]?.id ?? 'prompt';
    showView('result');
    renderTabs();
    showTab(state.activeTab);
    refreshSummary();
    return;
  }
  if (msg.type === 'PANEL_SCAN_PROMPT') {
    state.promptMeta = msg.prompt || state.promptMeta;
    renderTabs();
    if (state.activeTab === 'prompt') showTab('prompt');
    return;
  }
  if (msg.type === 'PANEL_SCAN_ITEM') {
    const at = state.docs.findIndex(d => d.id === msg.doc?.id);
    if (at >= 0) state.docs[at] = msg.doc;
    renderTabs();
    if (state.activeTab === msg.doc?.id) showTab(msg.doc.id);
    refreshSummary();
    return;
  }
  if (msg.type === 'PANEL_SCAN_DONE') {
    state.docs = msg.docs || state.docs;
    state.promptMeta = msg.prompt || state.promptMeta;
    renderTabs();
    refreshSummary();
    return;
  }

  if (msg.type === 'PANEL_PROGRESS') {
    state.sessionId = msg.sessionId;
    state.seq = msg.seq ?? state.seq;
    applyProgress(msg.event);
  } else if (msg.type === 'PANEL_RESULT') {
    state.sessionId = msg.sessionId;
    state.seq = msg.seq ?? state.seq;
    renderResult(msg.kind, msg.result, msg.meta);
  } else if (msg.type === 'PANEL_ERROR') {
    state.sessionId = msg.sessionId;
    state.seq = msg.seq ?? state.seq;
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
    state.seq = s.seq ?? null;
    if (s.status === 'ready' && s.result) renderResult(s.kind, s.result, s.meta);
    else if (s.status === 'error') renderError(s.error, s.meta);
    else renderProgress(s);
  });
}

renderProgress(null);
pullSnapshot();
