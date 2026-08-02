'use strict';
/* renderer/tray.js — 트레이 팝오버 로직 (Figma hMO6k051z9JXBoa2fWySND 노드 36:2846 1:1 반영) */

const t = window.tray;
const $ = (s) => document.querySelector(s);

// macOS 는 main/tray.js 의 BrowserWindow vibrancy(네이티브 글래스)가 적용되므로 CSS 배경을
// 옅게 낮춰 실제로 비치게 한다(styles 의 body.mac-glass 참고). Windows 는 vibrancy 가 없어
// 기존 진한 CSS 배경 그대로 둔다.
if (/mac/i.test(navigator.platform)) document.body.classList.add('mac-glass');

async function refresh() {
  const [engine, stats, metrics] = await Promise.all([
    t.getEngineStatus(), t.getStats(), t.getMetrics(),
  ]);
  paintEngine(engine, stats);
  paintMetrics(metrics);
}

function paintEngine(engine, stats) {
  const running = engine.state === 'running';
  const models = (stats && stats.models) || {};
  setPill('#t-pii-pill', models.pii, running);
  setPill('#t-inj-pill', models.injection, running);

  $('#t-pii-today').textContent = stats && stats.pii ? stats.pii.today ?? '0' : '0';
  $('#t-inj-today').textContent = stats && stats.injection ? stats.injection.today ?? '0' : '0';
}

// Figma 디자인은 라벨이 항상 "PII model"/"INJECTION model" 고정 텍스트다(index.html 의
// 홈 화면 모델 pill 도 동일한 관례) — 상태는 라벨 문구가 아니라 점(dot) 색으로만 표현한다.
function setPill(pillSel, model, running) {
  const ready = model && model.ready && running;
  $(pillSel).classList.toggle('on', !!ready);
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

$('#t-more').addEventListener('click', () => t.openDashboard());
$('#t-quit').addEventListener('click', () => t.quitApp());

t.onEngineStatus((s) => t.getStats().then((st) => paintEngine(s, st)));
t.onMetrics((m) => paintMetrics(m));
t.onStats((s) => t.getEngineStatus().then((e) => paintEngine(e, s)));

refresh();
setInterval(refresh, 3000);
