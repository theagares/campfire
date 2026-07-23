'use strict';
/* renderer/tray.js — 트레이 팝오버 로직 (Figma hMO6k051z9JXBoa2fWySND 노드 36:2846 1:1 반영) */

const t = window.tray;
const $ = (s) => document.querySelector(s);

async function refresh() {
  const [engine, stats, metrics] = await Promise.all([
    t.getEngineStatus(), t.getStats(), t.getMetrics(),
  ]);
  paintEngine(engine, stats);
  paintMetrics(metrics);
}

function paintEngine(engine, stats) {
  const running = engine.state === 'running';
  const toggle = $('#sec-toggle');
  // disabled 상태면 off, 그 외(running/starting/error)면 on 의도로 표시
  const enabledIntent = engine.state !== 'disabled' && engine.state !== 'stopped';
  toggle.checked = enabledIntent;
  $('#sec-label').textContent = enabledIntent ? 'Security ON' : 'Security OFF';

  const models = (stats && stats.models) || {};
  setPill('#t-pii-pill', '#t-pii-dot', '#t-pii-label', models.pii, running);
  setPill('#t-inj-pill', '#t-inj-dot', '#t-inj-label', models.injection, running);

  $('#t-pii-today').textContent = stats && stats.pii ? stats.pii.today ?? '0' : '0';
  $('#t-inj-today').textContent = stats && stats.injection ? stats.injection.today ?? '0' : '0';
}

function setPill(pillSel, dotSel, labelSel, model, running) {
  const ready = model && model.ready && running;
  $(pillSel).classList.toggle('on', !!ready);
  $(labelSel).textContent = model ? model.label : (running ? '작동 중' : '중지됨');
}

const RES_ICON_EXT = { cpu: 'png', ram: 'png', gpu: 'png', vram: 'svg' };
function paintMetrics(m) {
  if (!m) return;
  const rows = [['cpu', 'CPU', m.cpu], ['gpu', 'GPU', m.gpu], ['ram', 'RAM', m.ram], ['vram', 'VRAM', m.vram]];
  $('#t-res').innerHTML = rows.map(([key, label, r]) => {
    const available = r && r.available;
    const pct = available ? (r.percent ?? 0) : 0;
    const cls = pct >= 85 ? 'hot' : pct >= 65 ? 'warn' : '';
    const sub = available && r.breakdown && r.breakdown.length
      ? r.breakdown.map((it) => {
          const val = it.pct != null ? `${it.pct}%` : `${it.gb}GB`;
          return `<div class="rb-sub-item"><span class="l">${it.label}</span><span class="v">${val}</span></div>`;
        }).join('')
      : '';
    return `
      <div class="res-block">
        <div class="rb-head">
          <div class="rb-icon"><img src="../assets/figma/dash-icon-${key}.${RES_ICON_EXT[key]}" alt="" /></div>
          <span class="rb-label">${label}</span>
          <span class="rb-pct ${available ? '' : 'na'}">${available ? pct + '%' : 'N/A'}</span>
        </div>
        <div class="rb-bar"><div class="fill ${cls}" style="width:${available ? pct : 0}%"></div></div>
        ${sub}
      </div>`;
  }).join('');
}

$('#sec-toggle').addEventListener('change', async (e) => {
  await t.setSecurityEnabled(e.target.checked);
  $('#sec-label').textContent = e.target.checked ? 'Security ON' : 'Security OFF';
});
$('#t-more').addEventListener('click', () => t.openDashboard());
$('#t-quit').addEventListener('click', () => t.quitApp());

t.onEngineStatus((s) => t.getStats().then((st) => paintEngine(s, st)));
t.onMetrics((m) => paintMetrics(m));
t.onStats((s) => t.getEngineStatus().then((e) => paintEngine(e, s)));

refresh();
setInterval(refresh, 3000);
