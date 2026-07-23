'use strict';
/* renderer/tray.js — 트레이 팝오버 로직 (PLAN §8) */

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
  $('#sec-label').textContent = enabledIntent ? 'ON' : 'OFF';

  const models = (stats && stats.models) || {};
  setPill('#t-pii-dot', '#t-pii-label', models.pii, running);
  setPill('#t-inj-dot', '#t-inj-label', models.injection, running);

  $('#t-pii-today').textContent = stats && stats.pii ? stats.pii.today ?? '–' : '–';
  $('#t-inj-today').textContent = stats && stats.injection ? stats.injection.today ?? '–' : '–';
}

function setPill(dotSel, labelSel, model, running) {
  const ready = model && model.ready && running;
  $(dotSel).className = 'dot ' + (ready ? 'on' : 'off');
  $(labelSel).textContent = model ? model.label : (running ? '작동 중' : '중지됨');
}

function paintMetrics(m) {
  if (!m) return;
  const rows = [['CPU', m.cpu], ['GPU', m.gpu], ['RAM', m.ram], ['VRAM', m.vram]];
  $('#t-res').innerHTML = rows.map(([l, r]) => {
    if (!r || !r.available) {
      return `<div class="res"><span class="l">${l}</span><div class="track"></div><span class="p na">N/A</span></div>`;
    }
    const pct = r.percent ?? 0;
    const cls = pct >= 85 ? 'hot' : pct >= 65 ? 'warn' : '';
    return `<div class="res"><span class="l">${l}</span><div class="track"><div class="fill ${cls}" style="width:${pct}%"></div></div><span class="p">${pct}%</span></div>`;
  }).join('');
}

$('#sec-toggle').addEventListener('change', async (e) => {
  await t.setSecurityEnabled(e.target.checked);
  $('#sec-label').textContent = e.target.checked ? 'ON' : 'OFF';
});
$('#t-more').addEventListener('click', () => t.openDashboard());
$('#t-quit').addEventListener('click', () => t.quitApp());

t.onEngineStatus((s) => t.getStats().then((st) => paintEngine(s, st)));
t.onMetrics((m) => paintMetrics(m));
t.onStats((s) => t.getEngineStatus().then((e) => paintEngine(e, s)));

refresh();
setInterval(refresh, 3000);
