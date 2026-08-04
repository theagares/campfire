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
  mcpInfo: null,
};

// ── 전역 배너(설정 모달 밖에서도 보여야 하는 알림 — 설치 직후 자동 모델 설치 진행/실패 등) ──
// tone: 'warn'(기본, 주황) — 실패·문제 / 'progress'(퍼플) — 진행 중인 정상 절차.
// 같은 배너를 재사용하므로 매번 명시적으로 정리한다(이전 호출의 톤이 남지 않게).
function showGlobalBanner(text, tone = 'warn') {
  const el = $('#global-banner');
  $('#global-banner-text').textContent = text;
  el.classList.toggle('progress', tone === 'progress');
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
  if (view === 'connect') refreshMcpClients();
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
// MCP/확장 카드에는 "연결됨 / 미연결" 배지가 없다. 둘 다 판정이 애매했기 때문이다:
// MCP 는 "엔진이 떠 있으니 /mcp 를 부를 수 있다"까지만 확인할 수 있었고(활성 세션
// 조회 API 가 없다), 확장은 최근 60초 안에 /health 요청이 왔는지로 추정해서 브라우저를
// 잠깐 안 쓰면 멀쩡히 설치된 확장이 "미연결"로 뒤집혔다. 실제 상태와 어긋나는 표시라
// 아예 없앴다 — MCP 클라이언트별 연결 상태(아래 renderMcpClients)는 설정 파일을
// 직접 읽어 판정하므로 정확하고, 그대로 남긴다.
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
    ${resBlock('vram', 'VRAM', vram, vram && vram.available ? vramValueHtml(vram) : null)}`;
}

/** 사용량을 알 수 있으면 used/total, 총량만 알면 총량만 — 없는 수치를 지어내지 않는다
 *  (macOS 외장 GPU 는 총량만 권한 없이 읽을 수 있다, system-metrics.js 참고). */
function vramValueHtml(vram) {
  if (vram.usedGb != null && vram.totalGb != null) {
    return `${vram.usedGb}GB <span class="sub">/ ${vram.totalGb}GB</span>`;
  }
  if (vram.totalGb != null) return `<span class="sub">총 </span>${vram.totalGb}GB`;
  if (vram.percent != null) return `${vram.percent}%`;
  return '—';
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
          <span class="rb-value">${available
            // reason 은 못 읽을 때만 쓰는 게 아니다 — 읽어냈을 때도 그 수치가 어느
            // GPU 의 무엇인지(예: "Apple M2 Pro — 통합 메모리") 툴팁으로 남긴다.
            ? `<span class="main" title="${escapeHtml(r?.reason || '')}">${valueHtml}</span>`
            // 왜 못 읽는지를 알려준다 — 그냥 N/A 만 뜨면 고장난 것처럼 보인다
            // (macOS 는 통합 메모리라 전용 VRAM 이 아예 없는 게 정상이다).
            : `<span class="na-reason" title="${escapeHtml(r?.reason || '')}">${r?.reason ? escapeHtml(r.reason) : 'N/A'}</span>`
          }</span>
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
    // data-edge: 실시간 처리 단계에 맞춰 이 구간에 빛을 흘리려면(renderPipeline)
    // 그릴 때마다 새로 만들어지는 path 를 다시 찾을 수 있어야 한다.
    return `<path d="${d}" data-edge="${pipeEdgeKey(e)}" class="${e.dashed ? 'dashed' : ''}" marker-end="url(#pipe-arrow)" />`;
  }).join('');
  // defs(화살촉)는 index.html 에 고정 정의돼 있어 매번 다시 그릴 필요 없이 path 만 교체.
  Array.from(svg.querySelectorAll('path')).forEach((p) => p.remove());
  svg.insertAdjacentHTML('beforeend', paths);
  // 리사이즈·드래그로 다시 그린 뒤에도 현재 단계 표시가 유지되게 상태를 재적용한다.
  if (typeof reapplyPipeEdgeState === 'function') reapplyPipeEdgeState();
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

// ── 처리현황 실시간 동기화 ────────────────────────────────────────────────────
// 엔진의 GET /activity/stream(SSE)을 메인 프로세스가 중계해 pipeline:activity 로
// 넘겨준다. 여기서는 그걸 "지금 어느 구간을 지나고 있는가" 로 바꿔 보여준다.
//
// 표현 방식: 진행 중인 구간의 연결선 위로 빛이 흘러간다. 처음엔 노드 뒤에 후광만
// 넣었는데 --purple-soft(12% 투명도)라 사실상 안 보였고, 무엇보다 "어디까지 갔는지"
// 가 안 읽혔다. 선을 따라 움직이는 빛이 흐름을 훨씬 직관적으로 보여준다.
//
// 왜 SVG dash 애니메이션이 아니라 HTML 발광체인가: 이 SVG 는 viewBox 100x100 을
// preserveAspectRatio="none" 으로 460x220 캔버스에 늘려 쓰고 path 는
// vector-effect:non-scaling-stroke 다. 그래서 stroke-dasharray 는 화면 픽셀 기준인데
// getTotalLength() 는 user unit 을 돌려줘 둘이 안 맞고, 늘어난 좌표계 탓에 점도
// 타원으로 찌그러진다. path.getPointAtLength() 로 좌표만 얻어 HTML div 를 옮기면
// 발광체는 px 로 그려져 항상 동그랗고, 좌표는 % 라 캔버스가 늘어나도 선 위에 붙는다.
const PIPE_STAGE_ORDER = ['receive', 'parse', 'pii', 'injection', 'mask'];
const PIPE_STAGE_NODES = {
  receive: ['receive'],
  parse: ['parse'],
  pii: ['pii-detect'],
  injection: ['inj-detect'],
  mask: ['pii-done', 'inj-done'],
};
// 각 단계에서 "빛이 흐르는" 구간 = 그 단계로 들어오는 연결선.
// receive 는 들어오는 선이 없어 노드만 빛난다.
const PIPE_STAGE_EDGES = {
  parse: ['receive>parse'],
  pii: ['parse>pii-detect'],
  injection: ['parse>inj-detect'],
  mask: ['pii-detect>pii-done', 'inj-detect>inj-done'],
};
const pipeEdgeKey = (e) => `${e.from}>${e.to}`;

// 완료 직후 곧바로 회색으로 돌아가면 마지막 단계가 깜빡이고 끝나 눈에 안 남는다 —
// 잠깐 "완료" 상태를 보여주고 사그라들게 한다.
const PIPE_SETTLE_MS = 1400;
const PIPE_FLOW_PERIOD_MS = 1100; // 빛 하나가 구간을 지나는 데 걸리는 시간
const PIPE_SPARK_TRAIL = [0, 0.055, 0.11]; // 꼬리: 선행 빛 뒤로 경로 비율만큼 뒤처짐

const pipeActivity = { jobs: new Map(), settleTimer: null, settling: false };
const pipeFlow = { edges: new Set(), sparks: new Map(), raf: 0 };

function pipeStageIndex(stage) {
  const i = PIPE_STAGE_ORDER.indexOf(stage);
  return i === -1 ? -1 : i;
}

function applyPipelineActivity(ev) {
  if (!ev) return;
  if (ev.type === 'snapshot') {
    // 처리 도중에 앱을 켠 경우 — 현재 진행 중인 것들로 바로 맞춘다.
    pipeActivity.jobs.clear();
    (ev.active || []).forEach((a) => pipeActivity.jobs.set(a.jobId, a.stage));
  } else if (ev.type === 'activity') {
    if (ev.phase === 'finish') pipeActivity.jobs.delete(ev.jobId);
    else pipeActivity.jobs.set(ev.jobId, ev.stage);
  } else {
    return;
  }

  clearTimeout(pipeActivity.settleTimer);
  if (pipeActivity.jobs.size === 0) {
    // 방금 끝났다 — 잠깐 완료 상태를 유지한 뒤 idle 로.
    pipeActivity.settling = true;
    pipeActivity.settleTimer = setTimeout(() => {
      pipeActivity.settling = false;
      renderPipeline();
    }, PIPE_SETTLE_MS);
  } else {
    pipeActivity.settling = false;
  }
  renderPipeline();
}

// ── 흐르는 빛 ────────────────────────────────────────────────────────────────
function ensureSparks(edgeKey) {
  let sparks = pipeFlow.sparks.get(edgeKey);
  if (sparks) return sparks;
  const canvas = $('#pipe-diagram');
  if (!canvas) return null;
  sparks = PIPE_SPARK_TRAIL.map((_, i) => {
    const el = document.createElement('div');
    el.className = 'pipe-spark' + (i === 0 ? ' head' : '');
    canvas.appendChild(el);
    return el;
  });
  pipeFlow.sparks.set(edgeKey, sparks);
  return sparks;
}

function clearSparks(edgeKey) {
  const sparks = pipeFlow.sparks.get(edgeKey);
  if (!sparks) return;
  sparks.forEach((el) => el.remove());
  pipeFlow.sparks.delete(edgeKey);
}

function setFlowEdges(keys) {
  const next = new Set(keys);
  // 더 이상 흐르지 않는 구간의 발광체 제거
  for (const key of [...pipeFlow.sparks.keys()]) {
    if (!next.has(key)) clearSparks(key);
  }
  pipeFlow.edges = next;
  if (next.size === 0) {
    cancelAnimationFrame(pipeFlow.raf);
    pipeFlow.raf = 0;
    return;
  }
  if (!pipeFlow.raf) pipeFlow.raf = requestAnimationFrame(stepFlow);
}

/** 리사이즈·드래그로 path 를 다시 그린 직후, 현재 단계 표시(lit)를 새 path 에 다시 입힌다. */
function reapplyPipeEdgeState() {
  if (!pipeFlow.lit) return;
  PIPE_EDGES.forEach((e) => {
    const key = pipeEdgeKey(e);
    const path = $(`#pipe-lines path[data-edge="${key}"]`);
    if (path) path.classList.toggle('lit', pipeFlow.lit.has(key));
  });
}

function stepFlow(now) {
  pipeFlow.raf = 0;
  if (pipeFlow.edges.size === 0) return;

  // 처리현황 화면이 아니면 좌표 계산을 건너뛴다(보이지도 않는데 매 프레임 돌 이유가 없다).
  const visible = $('#view-pipeline')?.classList.contains('active');
  if (visible) {
    const t = (now % PIPE_FLOW_PERIOD_MS) / PIPE_FLOW_PERIOD_MS;
    pipeFlow.edges.forEach((key) => {
      const path = $(`#pipe-lines path[data-edge="${key}"]`);
      const sparks = ensureSparks(key);
      if (!path || !sparks) return;
      let len = 0;
      try { len = path.getTotalLength(); } catch { return; }
      if (!len) return;
      sparks.forEach((el, i) => {
        // 꼬리는 선행 빛보다 뒤처지고, 경로 앞에서 다시 나타나지 않게 잘라낸다.
        const at = t - PIPE_SPARK_TRAIL[i];
        if (at < 0) { el.style.opacity = '0'; return; }
        const p = path.getPointAtLength(at * len);
        el.style.left = p.x + '%';
        el.style.top = p.y + '%';
        el.style.opacity = String(1 - i * 0.32);
      });
    });
  }
  pipeFlow.raf = requestAnimationFrame(stepFlow);
}

function renderPipeline() {
  buildPipelineDiagram();

  const busy = pipeActivity.jobs.size > 0;
  // 여러 요청이 겹치면 가장 앞선 단계를 기준으로 보여준다(전체 흐름이 어디까지
  // 갔는지가 관심사라, 뒤처진 job 때문에 되감기는 것보다 자연스럽다).
  let furthest = -1;
  pipeActivity.jobs.forEach((stage) => {
    furthest = Math.max(furthest, pipeStageIndex(stage));
  });

  PIPE_NODES.forEach((n) => {
    const el = pipeNodeEls[n.id];
    if (!el) return;
    let cls = '';
    if (busy) {
      const stageOfNode = PIPE_STAGE_ORDER.find((s) => PIPE_STAGE_NODES[s]?.includes(n.id));
      const idx = pipeStageIndex(stageOfNode);
      if (idx !== -1 && idx < furthest) cls = 'done';
      else if (idx !== -1 && idx === furthest) cls = 'active';
    } else if (pipeActivity.settling) {
      if (PIPE_STAGE_ORDER.some((s) => PIPE_STAGE_NODES[s]?.includes(n.id))) cls = 'done';
    }
    el.classList.toggle('active', cls === 'active');
    el.classList.toggle('done', cls === 'done');
  });

  // 이미 지나온 구간은 계속 켜두고(어디까지 왔는지 남는다), 지금 구간만 빛이 흐른다.
  const currentStage = furthest === -1 ? null : PIPE_STAGE_ORDER[furthest];
  const flowing = busy && currentStage ? (PIPE_STAGE_EDGES[currentStage] || []) : [];
  const passed = new Set();
  if (busy || pipeActivity.settling) {
    const upto = pipeActivity.settling ? PIPE_STAGE_ORDER.length : furthest;
    PIPE_STAGE_ORDER.slice(0, Math.max(upto, 0)).forEach((s) => {
      (PIPE_STAGE_EDGES[s] || []).forEach((k) => passed.add(k));
    });
  }
  pipeFlow.lit = new Set([...passed, ...flowing]);
  reapplyPipeEdgeState();
  setFlowEdges(flowing);

  const running = busy;
  const status = $('#pipe-status');
  status.classList.toggle('running', running);
  status.classList.toggle('idle', !running);
  $('#pipe-status-label').textContent = running ? '탐지중' : '탐지 종료';
}

// ── 설정 모달 (PLAN §8) ───────────────────────────────────────────────────────
const modal = $('#settings-modal');
let draftPolicy = 'mask';

function openSettings() {
  const s = state.settings || {};
  draftPolicy = s.injectionPolicy || 'mask';
  $$('#policy-seg button').forEach((b) => b.classList.toggle('active', b.dataset.policy === draftPolicy));
  $('#detector-progress').style.display = 'none';
  $('#remote-url').value = s.remoteUrl || '';
  $('#upstage-api-key').value = s.upstageApiKey || '';
  $('#settings-port').textContent = (state.engine && state.engine.port) || '자동 관리';
  modal.classList.add('open');
}
function closeSettings() { modal.classList.remove('open'); }
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
    if (!modal.classList.contains('open')) showGlobalBanner(label, 'progress');
  } else if (ev.type === 'error' || ev.type === 'fallback') {
    showGlobalBanner(ev.message || '모델 설치 중 문제가 발생했습니다.');
  } else if (ev.type === 'done') {
    hideGlobalBanner();
  }
});

$('#settings-save').addEventListener('click', async () => {
  const remoteUrl = $('#remote-url').value.trim() || undefined;
  const patch = { injectionPolicy: draftPolicy };
  if (remoteUrl) patch.remoteUrl = remoteUrl;
  // remoteUrl 과 달리 API 키는 사용자가 "완전히 비워서 지우기"도 할 수 있어야 하므로
  // 빈 문자열도 patch 에 포함시킨다(trim() 만 하고 빈 값이어도 그대로 실어보냄).
  patch.upstageApiKey = $('#upstage-api-key').value.trim();

  // 탐지 모델은 더 이상 여기서 고를 게 없다(encoder/llm_mcp 고정, 룰베이스 폴백
  // 제거). 모델 다운로드는 main.js 가 기동 시 자동으로 트리거한다.
  state.settings = await api.setSettings(patch);
  setDetectorProgress(null);
  closeSettings();
  // 정책 변경 시 엔진 재시작이 트리거됨 → 상태는 push 로 갱신됨
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
  renderPipeline();
}

async function init() {
  const info = await api.getAppInfo().catch(() => null);
  if (info && info.dev) document.title = 'Campfire (dev)';

  state.settings = await api.getSettings();
  state.engine = await api.getEngineStatus();
  state.stats = await api.getStats();
  state.metrics = await api.getMetrics();

  renderAll();

  api.onEngineStatus((s) => { state.engine = s; renderHome(); renderDashboard(); });
  api.onMetrics((m) => { state.metrics = m; renderResources($('#db-resources')); });
  api.onStats((s) => { state.stats = s; renderHome(); renderDashboard(); });
  api.onNavigate((view) => goto(view));
  // 구버전 preload 와 섞여 실행될 수 있어 옵셔널 호출 — 없으면 처리현황은 idle 로 남는다.
  api.onPipelineActivity?.((ev) => applyPipelineActivity(ev));

  // MCP 클라이언트 목록은 주기적으로 갱신(사용자가 앱 밖에서 설정을 바꿀 수 있다)
  refreshMcpClients();
  setInterval(refreshMcpClients, 5000);
}

init().catch((err) => console.error('[renderer] init 실패:', err));
