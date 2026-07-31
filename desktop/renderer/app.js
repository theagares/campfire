'use strict';
/* renderer/app.js — 대시보드 렌더러 로직 (Figma 완성본 hMO6k051z9JXBoa2fWySND 1:1 반영) */

const api = window.upsec;
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  settings: null,
  engine: null,
  stats: null,
  metrics: null,
  connections: null,
  mcpInfo: null,
};

// ── 전역 배너(설정 모달 밖에서도 보여야 하는 알림 — 설치 직후 자동 모델 설치 진행/실패 등) ──
function showGlobalBanner(text) {
  const el = $('#global-banner');
  $('#global-banner-text').textContent = text;
  el.style.display = 'flex';
}
function hideGlobalBanner() {
  $('#global-banner').style.display = 'none';
}
$('#global-banner-close').addEventListener('click', hideGlobalBanner);

// ── 뷰 라우팅 ────────────────────────────────────────────────────────────────
function goto(view) {
  $$('.nav-item').forEach((b) => b.classList.toggle('active', b.dataset.view === view));
  $$('.view').forEach((v) => v.classList.toggle('active', v.id === `view-${view}`));
  if (view === 'connect') { refreshConnections(); refreshMcpClients(); }
}
$$('.nav-item').forEach((b) => b.addEventListener('click', () => goto(b.dataset.view)));

// ── 홈 ───────────────────────────────────────────────────────────────────────
function renderHome() {
  const models = (state.stats && state.stats.models) || null;
  setModelPill('#home-pii', models && models.pii);
  setModelPill('#home-inj', models && models.injection);
}

function setModelPill(sel, model) {
  const el = $(sel);
  if (!el) return;
  const ready = model && model.ready;
  el.classList.toggle('on', !!ready);
  el.classList.toggle('off', !ready);
}

// ── 연결 ─────────────────────────────────────────────────────────────────────
async function refreshConnections() {
  state.connections = await api.getConnections();
  renderConnections();
}
function renderConnections() {
  const c = state.connections;
  if (!c) return;
  const mcpCard = $('#conn-mcp-card');
  const mcpOn = c.mcp.status === 'available';
  mcpCard.classList.toggle('connected', mcpOn);
  mcpCard.classList.toggle('disconnected', !mcpOn);
  const mcpBadge = $('#conn-mcp-badge');
  mcpBadge.className = mcpOn ? 'badge-pill' : 'badge-plain';
  mcpBadge.textContent = mcpOn ? '연결됨' : '미연결';

  // 확장의 최근 /health 요청(Origin: chrome-extension://...)을 엔진이 감지해 알려주는
  // extensionLastSeenSecondsAgo 기준 — MCP 카드와 동일한 패턴(§connections.js 참고).
  const extCard = $('#conn-ext-card');
  const extOn = c.extension.status === 'available';
  extCard.classList.toggle('connected', extOn);
  extCard.classList.toggle('disconnected', !extOn);
  const extBadge = $('#conn-ext-badge');
  extBadge.className = extOn ? 'badge-pill' : 'badge-plain';
  extBadge.textContent = extOn ? '연결됨' : '미연결';
}
$('#conn-ext-install').addEventListener('click', async (e) => {
  const btn = e.currentTarget;
  btn.disabled = true;
  try {
    await api.openExtensionFolder();
  } catch (err) {
    showGlobalBanner(`확장 프로그램 폴더를 열지 못했습니다: ${err.message || err}`);
  } finally {
    btn.disabled = false;
  }
});

// ── MCP 클라이언트 원클릭 연결 ─────────────────────────────────────────────────
async function refreshMcpClients() {
  const info = await api.detectMcpClients().catch(() => ({ mcpUrl: null, clients: [] }));
  state.mcpInfo = info;
  renderMcpClients();
}
function renderMcpClients() {
  const list = $('#mcp-client-list');
  const info = state.mcpInfo;
  if (!list) return;
  if (!info || !info.mcpUrl) {
    list.innerHTML = `<div class="hint">엔진이 실행 중이어야 MCP 서버 URL을 확인할 수 있습니다.</div>`;
    return;
  }
  list.innerHTML = info.clients.map((c) => {
    if (c.method === 'manual') {
      return `
        <div class="mcp-client-row manual" data-id="${c.id}">
          <div class="mcp-client-head"><span class="mcp-client-name">${c.name}</span><span class="badge-plain">수동</span></div>
          <div class="hint">${escapeHtml(c.hint)}</div>
          <div class="codeblock-row"><code>${escapeHtml(c.snippet)}</code><button class="copy-btn" data-copy="${btoa(unescape(encodeURIComponent(c.snippet)))}"><img src="../assets/figma/copy-icon.svg" alt="copy" /></button></div>
        </div>`;
    }
    const statusText = !c.available ? '미설치' : c.connected ? '연결됨' : '미연결';
    const actionLabel = c.connected ? '연결 해제' : '연결하기';
    return `
      <div class="mcp-client-row" data-id="${c.id}">
        <div class="mcp-client-head">
          <span class="mcp-client-name">${c.name}</span>
          <span class="badge-plain">${statusText}</span>
        </div>
        <button class="conn-link mcp-action-btn" data-id="${c.id}" data-connected="${!!c.connected}" ${c.available ? '' : 'disabled'}>${actionLabel}</button>
      </div>`;
  }).join('');
}
$('#mcp-client-list').addEventListener('click', async (e) => {
  const copyBtn = e.target.closest('.copy-btn');
  if (copyBtn) {
    const snippet = decodeURIComponent(escape(atob(copyBtn.dataset.copy || '')));
    navigator.clipboard.writeText(snippet).catch(() => {});
    return;
  }
  const actionBtn = e.target.closest('.mcp-action-btn');
  if (!actionBtn || actionBtn.disabled) return;
  const id = actionBtn.dataset.id;
  const connected = actionBtn.dataset.connected === 'true';
  actionBtn.disabled = true;
  const originalLabel = actionBtn.textContent;
  actionBtn.textContent = '처리 중...';
  try {
    if (connected) await api.disconnectMcpClient(id);
    else await api.connectMcpClient(id);
  } catch (err) {
    showGlobalBanner(`MCP 연결 실패(${id}): ${err.message || err}`);
  }
  actionBtn.textContent = originalLabel;
  await refreshMcpClients();
});

// ── 대시보드 ──────────────────────────────────────────────────────────────────
function renderDashboard() {
  const s = state.stats;
  const banner = $('#stats-source-banner');
  if (!s) return;

  if (s.available === false) {
    banner.innerHTML =
      `<div class="banner warn">⚠️ 실측 통계를 읽을 수 없어 값이 비어 있습니다. (사유: ${escapeHtml(s.reason || '알 수 없음')})</div>`;
  } else {
    banner.innerHTML = '';
  }

  const pii = s.pii || {};
  const inj = s.injection || {};
  $('#tile-pii-today').textContent = fmt(pii.today);
  $('#tile-pii-week').textContent = fmt(pii.week);
  $('#tile-pii-month').textContent = fmt(pii.month);
  $('#tile-pii-total').textContent = fmt(pii.total);
  $('#tile-inj-today').textContent = fmt(inj.today);
  $('#tile-inj-week').textContent = fmt(inj.week);
  $('#tile-inj-month').textContent = fmt(inj.month);
  $('#tile-inj-total').textContent = fmt(inj.total);

  // 모델 상태
  const m = s.models || {};
  setModelDot('#dash-pii-dot', '#dash-pii-label', m.pii, 'A.X Encoder base 0.1B');
  setModelDot('#dash-inj-dot', '#dash-inj-label', m.injection, 'EXAONE 4.0 1.2B');

  // 최근 14일 탐지 추이(engine store 실측, main/engine-stats.js trend) — 오늘 버킷은
  // 새 탐지가 기록될 때마다 값이 올라가므로, 5초 폴링(onStats)만으로도 실시간처럼
  // 그래프가 갱신된다. 예전엔 데이터와 무관한 장식용 고정 이미지였다.
  renderTrendChart('#dc-chart-pii', s.trend, 'pii', '#584CDC');
  renderTrendChart('#dc-chart-inj', s.trend, 'injection', '#F04452');

  renderResources($('#db-resources'));
}
function renderTrendChart(svgSel, trend, key, color) {
  const svg = $(svgSel);
  if (!svg) return;
  const values = (trend || []).map((t) => Number(t[key]) || 0);
  if (!values.length) { svg.innerHTML = ''; return; }

  const max = Math.max(1, ...values);
  const n = values.length;
  const points = values.map((v, i) => {
    const x = n > 1 ? (i / (n - 1)) * 100 : 100;
    const y = 96 - (v / max) * 92; // 위 4% / 아래 4% 여백
    return [x, y];
  });
  const line = points.map(([x, y], i) => `${i === 0 ? 'M' : 'L'}${x.toFixed(2)},${y.toFixed(2)}`).join(' ');
  const area = `${line} L100,100 L0,100 Z`;
  const [lastX, lastY] = points[points.length - 1];

  svg.innerHTML = `
    <path class="dc-chart-area" d="${area}" fill="${color}"></path>
    <path class="dc-chart-line" d="${line}" stroke="${color}"></path>
    <circle class="dc-chart-dot" cx="${lastX.toFixed(2)}" cy="${lastY.toFixed(2)}" r="2.4" fill="${color}"></circle>
  `;
}
function setModelDot(dotSel, labelSel, model, fallbackCaption) {
  const ready = model && model.ready;
  // classList.toggle(force) 는 이미 그 상태면 DOM을 건드리지 않는다 — className을
  // 매번 통째로 다시 대입하면(이전 버전) 값이 그대로여도 속성 변경으로 처리되어
  // glow CSS 애니메이션이 5초 폴링마다 처음부터 재시작해 버렸다(실측: "빛이 생길 때
  // 사라지기도 함"의 원인). setModelPill과 동일한 관용구로 통일.
  const dot = $(dotSel);
  dot.classList.toggle('on', !!ready);
  dot.classList.toggle('off', !ready);
  $(labelSel).textContent = model ? `${model.name} · ${model.label}` : fallbackCaption;
}

function renderResources(host) {
  const m = state.metrics;
  if (!host) return;
  const cpu = m && m.cpu;
  const ram = m && m.ram;
  const gpu = m && m.gpu;
  const vram = m && m.vram;

  host.innerHTML = `
    <div class="res-divider"></div>
    ${resBlock('cpu', 'CPU', cpu, cpu && `${cpu.percent}%`)}
    ${resBlock('ram', 'RAM', ram, ram && `${ram.usedGb}GB <span class="sub">/ ${ram.totalGb}GB</span>`)}
    ${resBlock('gpu', 'GPU', gpu, gpu && gpu.available ? `${gpu.percent}%` : null)}
    ${resBlock('vram', 'VRAM', vram, vram && vram.available
      ? (vram.usedGb != null ? `${vram.usedGb}GB <span class="sub">/ ${vram.totalGb}GB</span>` : `${vram.percent}%`)
      : null)}`;
}
const RES_ICON_EXT = { cpu: 'png', ram: 'png', gpu: 'png', vram: 'svg' };
function resBlock(key, label, r, valueHtml) {
  const available = r && r.available;
  const pct = available ? (r.percent ?? 0) : 0;
  const cls = pct >= 85 ? 'hot' : pct >= 65 ? 'warn' : '';
  return `
    <div class="res-block ${key} ${available ? '' : 'na'}">
      <div class="rb-head">
        <div class="rb-icon"><img src="../assets/figma/dash-icon-${key}.${RES_ICON_EXT[key]}" alt="" /></div>
        <div class="rb-top">
          <span class="rb-label">${label}</span>
          <span class="rb-value">${available ? `<span class="main">${valueHtml}</span>` : 'N/A'}</span>
        </div>
      </div>
      <div class="rb-body">
        <div class="rb-bar"><div class="fill ${cls}" style="width:${available ? pct : 0}%"></div></div>
        ${subRow(available && r.breakdown)}
      </div>
    </div>`;
}

// breakdown 은 system-metrics.js 가 실측 가능할 때만 채워준다(CPU/RAM 은 항상,
// GPU/VRAM 은 nvidia-smi 사용 가능 시에만) — 없으면 이 줄은 그냥 비워둔다(가짜 수치 금지).
// Figma 처럼 세로 나열 + 좌측 정렬.
function subRow(items) {
  if (!items || !items.length) return '';
  const rows = items.map((it) => {
    const val = it.pct != null ? `${it.pct}%` : `${it.gb}GB`;
    return `<div class="rb-sub-item"><span class="rb-sub-label">${it.label}</span><span class="rb-sub-value">${val}</span></div>`;
  });
  return `<div class="rb-sub">${rows.join('')}</div>`;
}

// ── 처리현황 (드래그 가능한 다이어그램, PLAN §8 확정: "요소를 드래그해 위치를 조정"은
// 디자인 표현이 아니라 실제 구현할 기능) ─────────────────────────────────────
// 좌표는 Figma 노드(120:481 그룹, 카드 원점 기준 66,94)를 460×220 캔버스로 환산하고,
// "탐지 종류 부분이 힌트 문구에 붙어 보인다"는 피드백을 반영해 y를 전체적으로 +15 내렸다.
const PIPE_NODES = [
  { id:'receive', src:'receive', label:null, left:0,      top:25.45, w:9.53, h:19.93 },
  { id:'parse',   src:'parse', ext:'svg', label:null, left:34.13, top:25, w:18.91, h:20 },
  { id:'pii-detect', src:'pii-detect', label:'PII 탐지', left:62.77, top:6.82, w:9.53, h:19.93 },
  { id:'inj-detect', src:'inj-detect', label:'INJECTION 탐지', left:62.77, top:44.88, w:9.53, h:19.93 },
  { id:'pii-done', src:'pii-done', label:'PII 마스킹 완료', left:88.81, top:6.82, w:9.53, h:19.93 },
  { id:'inj-done', src:'inj-done', label:'INJECTION 차단 완료', left:88.77, top:45.64, w:9.53, h:19.93 },
  { id:'text-extract', src:'text-extract', label:'텍스트\n직접 추출', left:39.3, top:64.2, w:9.78, h:20.45 },
];
const PIPE_EDGES = [
  { from:'receive', to:'parse' },
  { from:'parse', to:'pii-detect' },
  { from:'parse', to:'inj-detect' },
  { from:'pii-detect', to:'pii-done' },
  { from:'inj-detect', to:'inj-done' },
  { from:'text-extract', to:'parse', dashed:true },
];
const pipeNodeEls = {};
let pipeDiagramBuilt = false;
let pipeSaveTimer = null;

function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

function buildPipelineDiagram() {
  if (pipeDiagramBuilt) return;
  pipeDiagramBuilt = true;
  const canvas = $('#pipe-diagram');
  const svg = $('#pipe-lines');
  const saved = (state.settings && state.settings.pipelineLayout) || {};

  PIPE_NODES.forEach((n) => {
    const pos = saved[n.id] || { left: n.left, top: n.top };
    const el = document.createElement('div');
    el.className = 'pipe-node';
    el.dataset.id = n.id;
    el.style.left = pos.left + '%';
    el.style.top = pos.top + '%';
    el.style.width = n.w + '%';
    el.style.height = n.h + '%';
    const label = n.label ? `<div class="pn-label">${n.label}</div>` : '';
    const ext = n.ext || 'png';
    el.innerHTML = `<img class="pn-icon" src="../assets/figma/pipe-node-${n.src}.${ext}" alt="${n.label || n.id}" />${label}`;
    canvas.appendChild(el);
    pipeNodeEls[n.id] = el;
    makePipeNodeDraggable(el, canvas);
  });

  window.addEventListener('resize', drawPipeLines);
  drawPipeLines();
}

// 위/좌/우는 카드(.pipe-card) 전체 기준 5% 여백만 남기고, 아래쪽은 구분선(.pc-divider)
// 바로 위까지만 움직이게 한다 — 라벨이 있는 노드(텍스트 직접 추출 등)는 라벨 높이까지
// 감안해야 실제로 선과 안 겹친다. 고정 퍼센트를 미리 계산해두는 대신 드래그를 시작할
// 때마다 카드·캔버스·구분선·라벨의 실측 픽셀 위치를 다시 재서 정확히 맞춘다(창 크기가
// 달라져도 항상 맞는다). 노드 좌표(left/top)는 캔버스(.pipe-diagram) 기준 %라서, "카드
// 기준 5%"를 캔버스 좌표계로 환산하면 캔버스가 카드보다 안쪽에 있는 만큼 음수/100% 초과
// 값이 나올 수 있는데 — 그게 정상이다(카드 기준 여백이라 그렇다).
const PIPE_EDGE_MARGIN_PCT = 5;
const PIPE_DIVIDER_GAP_PX = 6;

function makePipeNodeDraggable(el, canvas) {
  el.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    el.setPointerCapture(e.pointerId);
    el.classList.add('dragging');
    const startX = e.clientX;
    const startY = e.clientY;
    const startLeft = parseFloat(el.style.left);
    const startTop = parseFloat(el.style.top);
    const w = parseFloat(el.style.width);
    const h = parseFloat(el.style.height);

    const canvasRect = canvas.getBoundingClientRect();
    const cardRect = canvas.closest('.pipe-card').getBoundingClientRect();
    const divider = document.querySelector('.pc-divider');
    const dividerTopPx = divider ? divider.getBoundingClientRect().top - canvasRect.top : canvasRect.height;
    const label = el.querySelector('.pn-label');
    const labelReservePx = label ? label.getBoundingClientRect().height + 3 : 0; // 3px = .pn-label margin-top
    const maxTopPx = dividerTopPx - labelReservePx - PIPE_DIVIDER_GAP_PX - (h / 100) * canvasRect.height;
    const maxTopPct = (maxTopPx / canvasRect.height) * 100;

    const cardMarginX = cardRect.width * (PIPE_EDGE_MARGIN_PCT / 100);
    const cardMarginY = cardRect.height * (PIPE_EDGE_MARGIN_PCT / 100);
    const minLeftPct = ((cardRect.left + cardMarginX - canvasRect.left) / canvasRect.width) * 100;
    const maxLeftPct = ((cardRect.right - cardMarginX - canvasRect.left) / canvasRect.width) * 100 - w;
    const minTopPct = ((cardRect.top + cardMarginY - canvasRect.top) / canvasRect.height) * 100;

    const move = (ev) => {
      const dxPct = ((ev.clientX - startX) / canvasRect.width) * 100;
      const dyPct = ((ev.clientY - startY) / canvasRect.height) * 100;
      el.style.left = clamp(startLeft + dxPct, minLeftPct, maxLeftPct) + '%';
      el.style.top = clamp(startTop + dyPct, minTopPct, maxTopPct) + '%';
      drawPipeLines();
    };
    const up = () => {
      el.classList.remove('dragging');
      el.releasePointerCapture(e.pointerId);
      document.removeEventListener('pointermove', move);
      document.removeEventListener('pointerup', up);
      schedulePipeLayoutSave();
    };
    document.addEventListener('pointermove', move);
    document.addEventListener('pointerup', up);
  });
}

function drawPipeLines() {
  const svg = $('#pipe-lines');
  if (!svg) return;
  const centers = {};
  PIPE_NODES.forEach((n) => {
    const el = pipeNodeEls[n.id];
    if (!el) return;
    const l = parseFloat(el.style.left), t = parseFloat(el.style.top);
    const w = parseFloat(el.style.width), h = parseFloat(el.style.height);
    centers[n.id] = { x: l + w / 2, y: t + h / 2 };
  });
  const paths = PIPE_EDGES.map((e) => {
    const a = centers[e.from], b = centers[e.to];
    if (!a || !b) return '';
    const dx = (b.x - a.x) / 2;
    const d = `M ${a.x},${a.y} C ${a.x + dx},${a.y} ${b.x - dx},${b.y} ${b.x},${b.y}`;
    return `<path d="${d}" class="${e.dashed ? 'dashed' : ''}" marker-end="url(#pipe-arrow)" />`;
  }).join('');
  // defs(화살촉)는 index.html 에 고정 정의돼 있어 매번 다시 그릴 필요 없이 path 만 교체.
  Array.from(svg.querySelectorAll('path')).forEach((p) => p.remove());
  svg.insertAdjacentHTML('beforeend', paths);
}

function schedulePipeLayoutSave() {
  clearTimeout(pipeSaveTimer);
  pipeSaveTimer = setTimeout(() => {
    const layout = {};
    PIPE_NODES.forEach((n) => {
      const el = pipeNodeEls[n.id];
      if (!el) return;
      layout[n.id] = { left: parseFloat(el.style.left), top: parseFloat(el.style.top) };
    });
    api.savePipelineLayout(layout).catch(() => {});
  }, 400);
}

function renderPipeline() {
  buildPipelineDiagram();
  // 실시간 "탐지중" 신호는 엔진 REST 계약(§2)에 없어 idle 고정 표시.
  const running = false;
  const status = $('#pipe-status');
  status.classList.toggle('running', running);
  status.classList.toggle('idle', !running);
  $('#pipe-status-label').textContent = running ? '탐지중' : '탐지 종료';
}

// ── 설정 모달 (PLAN §8) ───────────────────────────────────────────────────────
const modal = $('#settings-modal');
let draftPolicy = 'mask';
let draftDetectorMode = 'rule_based'; // 'rule_based' | 'advanced'(encoder+llm_mcp)
let detectorBusy = false;

function detectorModeOf(s) {
  return s.piiDetector === 'encoder' && s.injectionDetector === 'llm_mcp' ? 'advanced' : 'rule_based';
}

function openSettings() {
  const s = state.settings || {};
  draftPolicy = s.injectionPolicy || 'mask';
  draftDetectorMode = detectorModeOf(s);
  $$('#policy-seg button').forEach((b) => b.classList.toggle('active', b.dataset.policy === draftPolicy));
  $$('#detector-seg button').forEach((b) => b.classList.toggle('active', b.dataset.mode === draftDetectorMode));
  $('#detector-progress').style.display = 'none';
  $('#remote-url').value = s.remoteUrl || '';
  $('#upstage-api-key').value = s.upstageApiKey || '';
  $('#settings-port').textContent = (state.engine && state.engine.port) || '자동 관리';
  modal.classList.add('open');
}
function closeSettings() { if (!detectorBusy) modal.classList.remove('open'); }
$('#open-settings').addEventListener('click', openSettings);
$('#close-settings').addEventListener('click', closeSettings);
$('#settings-cancel').addEventListener('click', closeSettings);
modal.addEventListener('click', (e) => { if (e.target === modal) closeSettings(); });
$$('#policy-seg button').forEach((b) =>
  b.addEventListener('click', () => {
    draftPolicy = b.dataset.policy;
    $$('#policy-seg button').forEach((x) => x.classList.toggle('active', x === b));
  })
);
$$('#detector-seg button').forEach((b) =>
  b.addEventListener('click', () => {
    if (detectorBusy) return;
    draftDetectorMode = b.dataset.mode;
    $$('#detector-seg button').forEach((x) => x.classList.toggle('active', x === b));
  })
);

function setDetectorProgress(label) {
  const el = $('#detector-progress');
  if (label == null) { el.style.display = 'none'; el.textContent = ''; return; }
  el.style.display = 'block';
  el.textContent = label;
}

api.onModelsFetchProgress?.((ev) => {
  if (ev.type === 'progress') {
    const pct = ev.pct != null ? ` ${ev.pct}%` : '';
    const label = `${ev.label || ''}${pct}`;
    setDetectorProgress(label);
    // 설정 모달이 닫혀 있어도(설치 직후 자동 설치 루틴) 진행 상황을 보여준다.
    if (!modal.classList.contains('open')) showGlobalBanner(label);
  } else if (ev.type === 'error' || ev.type === 'fallback') {
    showGlobalBanner(ev.message || '모델 설치 중 문제가 발생했습니다.');
  } else if (ev.type === 'done') {
    hideGlobalBanner();
  }
});

$('#settings-save').addEventListener('click', async () => {
  if (detectorBusy) return;
  const remoteUrl = $('#remote-url').value.trim() || undefined;
  const patch = { injectionPolicy: draftPolicy };
  if (remoteUrl) patch.remoteUrl = remoteUrl;
  // remoteUrl 과 달리 API 키는 사용자가 "완전히 비워서 지우기"도 할 수 있어야 하므로
  // 빈 문자열도 patch 에 포함시킨다(trim() 만 하고 빈 값이어도 그대로 실어보냄).
  patch.upstageApiKey = $('#upstage-api-key').value.trim();

  if (draftDetectorMode === 'advanced') {
    detectorBusy = true;
    $('#settings-save').disabled = true;
    try {
      const status = await api.getModelsStatus();
      const needsDownload = !(status.pii?.ready && status.injection?.ready);
      if (needsDownload) {
        setDetectorProgress('모델 가중치 확인 중...');
        await api.fetchModels(); // 진행률은 onModelsFetchProgress 구독으로 갱신됨
      }
      patch.piiDetector = 'encoder';
      patch.injectionDetector = 'llm_mcp';
    } catch (err) {
      setDetectorProgress(`다운로드 실패: ${err.message || err} — 설정이 적용되지 않았습니다.`);
      detectorBusy = false;
      $('#settings-save').disabled = false;
      return;
    }
  } else {
    patch.piiDetector = 'rule_based';
    patch.injectionDetector = 'rule_based';
  }

  state.settings = await api.setSettings(patch);
  detectorBusy = false;
  $('#settings-save').disabled = false;
  setDetectorProgress(null);
  closeSettings();
  // 정책/detector 변경 시 엔진 재시작이 트리거됨 → 상태는 push 로 갱신됨
});

// ── 사이드바 접기 토글 (Figma 30:487 상단 버튼 — 인터랙션 미확정, 시각 요소만 반영) ──
$('#sidebar-toggle').addEventListener('click', () => {
  document.querySelector('.sidebar').classList.toggle('collapsed');
});

// ── 초기화 & 실시간 구독 ──────────────────────────────────────────────────────
function fmt(v) { return v == null ? '–' : String(v); }
function escapeHtml(s) { return String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }

function renderAll() {
  renderHome();
  renderDashboard();
  renderConnections();
  renderPipeline();
}

async function init() {
  const info = await api.getAppInfo().catch(() => null);
  if (info && info.dev) document.title = 'UpSecurity (dev)';

  state.settings = await api.getSettings();
  state.engine = await api.getEngineStatus();
  state.stats = await api.getStats();
  state.metrics = await api.getMetrics();

  renderAll();

  api.onEngineStatus((s) => { state.engine = s; renderHome(); renderDashboard(); });
  api.onMetrics((m) => { state.metrics = m; renderResources($('#db-resources')); });
  api.onStats((s) => { state.stats = s; renderHome(); renderDashboard(); });
  api.onNavigate((view) => goto(view));

  // 연결 상태는 주기적으로 갱신
  refreshConnections();
  refreshMcpClients();
  setInterval(refreshConnections, 5000);
  setInterval(refreshMcpClients, 5000);
}

init().catch((err) => console.error('[renderer] init 실패:', err));
