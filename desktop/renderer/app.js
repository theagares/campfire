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
};

// ── 뷰 라우팅 ────────────────────────────────────────────────────────────────
function goto(view) {
  $$('.nav-item').forEach((b) => b.classList.toggle('active', b.dataset.view === view));
  $$('.view').forEach((v) => v.classList.toggle('active', v.id === `view-${view}`));
  if (view === 'connect') refreshConnections();
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
  $('#conn-mcp-note').textContent = c.mcp.note || '';

  // 익스텐션 활성 연결은 앱에서 관측 불가 → 항상 'unknown'(§connections.js 주석).
  // Figma 문구 "미연결"은 확정적 부정 주장이라 정확하지 않으므로, 실제 상태를
  // 정직하게 반영하는 기존 문구를 유지한다(시각 스타일만 Figma의 무채색 배지에 맞춤).
  const extCard = $('#conn-ext-card');
  extCard.classList.add('disconnected');
  $('#conn-ext-badge').textContent = '상태 확인 불가';
  $('#conn-ext-note').textContent = c.extension.note || '';
}
$('#conn-ext-install').addEventListener('click', () => {
  const url = (state.connections && state.connections.extension.helpUrl) || '';
  if (url) api.openExternal(url);
});
$$('.codeblock-row .copy-btn').forEach((btn) => {
  btn.addEventListener('click', () => {
    const code = btn.parentElement.querySelector('code').textContent;
    navigator.clipboard.writeText(code).catch(() => {});
  });
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

  renderResources($('#db-resources'));
}
function setModelDot(dotSel, labelSel, model, fallbackCaption) {
  const ready = model && model.ready;
  $(dotSel).className = 'mc-dot ' + (ready ? 'on' : 'off');
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

// ── 처리현황 (Figma 정적 다이어그램, PLAN §8) ─────────────────────────────────
function renderPipeline() {
  // 실시간 "탐지중" 신호는 엔진 REST 계약(§2)에 없어 idle 고정 표시.
  // 실행중 상태 에셋(assets/figma/pipe-diagram-running.png)은 신호가 추가되면 그대로 교체 가능.
  const running = false;
  $('#pipe-diagram-img').src = running ? '../assets/figma/pipe-diagram-running.png' : '../assets/figma/pipe-diagram-idle.png';
  const status = $('#pipe-status');
  status.className = 'pc-status ' + (running ? 'running' : 'idle');
  $('#pipe-status-label').textContent = running ? '탐지중' : '탐지 종료';
}

// ── 설정 모달 (PLAN §8) ───────────────────────────────────────────────────────
const modal = $('#settings-modal');
let draftPolicy = 'mask';
function openSettings() {
  const s = state.settings || {};
  draftPolicy = s.injectionPolicy || 'mask';
  $$('#policy-seg button').forEach((b) => b.classList.toggle('active', b.dataset.policy === draftPolicy));
  $('#remote-url').value = s.remoteUrl || '';
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
$('#settings-save').addEventListener('click', async () => {
  const remoteUrl = $('#remote-url').value.trim() || undefined;
  const patch = { injectionPolicy: draftPolicy };
  if (remoteUrl) patch.remoteUrl = remoteUrl;
  state.settings = await api.setSettings(patch);
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
  setInterval(refreshConnections, 5000);
}

init().catch((err) => console.error('[renderer] init 실패:', err));
