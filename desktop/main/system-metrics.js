'use strict';
/**
 * main/system-metrics.js
 * 시스템 리소스 (PLAN §8 트레이/대시보드).
 *
 * CPU/RAM 기본값은 Node os 모듈 실측치(매 tick 동기 계산, 비용 거의 없음).
 * GPU/VRAM 실측(nvidia-smi/Windows 성능 카운터)과 macOS RAM 세부 분해(vm_stat)는
 * 서브프로세스를 띄워야 해서 메인 프로세스를 블로킹하지 않도록 백그라운드에서
 * 주기적으로만 갱신하고, sample()은 항상 마지막 캐시값을 즉시 반환한다(동기 유지 —
 * ipc.js/main.js 호출부를 async 로 바꿀 필요 없음).
 */

const os = require('os');
const { execFile } = require('child_process');

const HEAVY_REFRESH_MS = 8000; // GPU/VRAM 등 무거운 조회 주기(2초 tick 마다 하지 않음)

let prevCpu = null;
let heavyRefreshing = false;
let lastHeavyRefresh = 0;

let cachedGpu = { percent: null, available: false };
let cachedVram = { percent: null, available: false };
let cachedMacRamBreakdown = null; // { wiredGb, compressedGb, appGb } | null

function execFileP(cmd, args, opts) {
  return new Promise((resolve, reject) => {
    execFile(cmd, args, { timeout: 3000, windowsHide: true, ...opts }, (err, stdout) => {
      if (err) reject(err);
      else resolve(stdout);
    });
  });
}

/**
 * os.cpus()[i].times 는 코어별 user/nice/sys/idle/irq 누적(ms)을 준다 — 전부 실측치.
 * "시스템/사용자/대기" 세분화(Figma 63:258)는 이 값을 그대로 합산해 구한다(가짜 수치 아님).
 */
function cpuTimes() {
  const cpus = os.cpus();
  let user = 0;
  let sys = 0; // sys + nice + irq 를 "시스템"으로 묶음
  let idle = 0;
  let total = 0;
  for (const c of cpus) {
    for (const t of Object.values(c.times)) total += t;
    user += c.times.user;
    sys += c.times.sys + c.times.nice + c.times.irq;
    idle += c.times.idle;
  }
  return { user, sys, idle, total };
}

/** CPU 사용률(%) 및 시스템/사용자/대기 분해 — 이전 샘플과의 델타로 계산. 첫 호출은 0. */
function cpuPercent() {
  const cur = cpuTimes();
  if (!prevCpu) {
    prevCpu = cur;
    return { percent: 0, systemPct: 0, userPct: 0, idlePct: 100 };
  }
  const totalDelta = cur.total - prevCpu.total;
  const userDelta = cur.user - prevCpu.user;
  const sysDelta = cur.sys - prevCpu.sys;
  const idleDelta = cur.idle - prevCpu.idle;
  prevCpu = cur;
  if (totalDelta <= 0) return { percent: 0, systemPct: 0, userPct: 0, idlePct: 100 };
  const clamp = (v) => Math.max(0, Math.min(100, Math.round(v * 10) / 10));
  const userPct = clamp((userDelta / totalDelta) * 100);
  const systemPct = clamp((sysDelta / totalDelta) * 100);
  const idlePct = clamp((idleDelta / totalDelta) * 100);
  const percent = Math.max(0, Math.min(100, Math.round(100 - idlePct)));
  return { percent, systemPct, userPct, idlePct };
}

function ramPercent() {
  const total = os.totalmem();
  const free = os.freemem();
  if (total <= 0) return { percent: 0, usedGb: 0, freeGb: 0, totalGb: 0 };
  const used = total - free;
  return {
    percent: Math.round((used / total) * 100),
    usedGb: +(used / 1024 ** 3).toFixed(1),
    freeGb: +(free / 1024 ** 3).toFixed(1),
    totalGb: +(total / 1024 ** 3).toFixed(1),
  };
}

/**
 * RAM 세부 분해는 OS 마다 실제로 존재하는 개념이 다르다(동적 분기):
 *  - macOS: Wired/Compressed 는 커널이 실제로 구분 관리하는 실측 카테고리 → vm_stat 로 읽는다.
 *  - Windows/기타: "와이어드/압축됨" 같은 OS 레벨 개념 자체가 없어 지어낼 수 없음 →
 *    실측 가능한 사용 중/여유로 대체(§PLAN 원칙: 허위 수치 금지).
 */
function ramBreakdown(ram) {
  if (process.platform === 'darwin' && cachedMacRamBreakdown) {
    const { wiredGb, compressedGb } = cachedMacRamBreakdown;
    const appGb = Math.max(0, +(ram.usedGb - wiredGb - compressedGb).toFixed(1));
    return [
      { label: '앱', gb: appGb },
      { label: '와이어드', gb: wiredGb },
      { label: '압축됨', gb: compressedGb },
    ];
  }
  return [
    { label: '사용 중', gb: ram.usedGb },
    { label: '여유', gb: ram.freeGb },
  ];
}

/** macOS: vm_stat 파싱 → Wired/Compressed 실측(바이트). 실패 시 null(폴백은 ramBreakdown 이 처리). */
async function refreshMacRam() {
  const stdout = await execFileP('vm_stat', []);
  const pageMatch = stdout.match(/page size of (\d+) bytes/);
  const pageSize = pageMatch ? parseInt(pageMatch[1], 10) : 4096;
  const grab = (label) => {
    const m = stdout.match(new RegExp(label.replace(/\s/g, '\\s+') + ':\\s+(\\d+)\\.'));
    return m ? (parseInt(m[1], 10) * pageSize) / 1024 ** 3 : 0;
  };
  const wiredGb = +grab('Pages wired down').toFixed(1);
  // macOS 버전에 따라 라벨이 다름("occupied by compressor" 구버전 / "stored in compressor" 신버전)
  const compressedGb = +(grab('Pages occupied by compressor') || grab('Pages stored in compressor')).toFixed(1);
  cachedMacRamBreakdown = { wiredGb, compressedGb };
}

/**
 * Windows: nvidia-smi 우선 시도 — NVIDIA 카드는 정확한 사용률/VRAM 을 그대로 얻는다.
 * utilization.gpu(코어)와 utilization.memory(메모리 컨트롤러)는 서로 다른 실측 지표라
 * GPU 의 "코어/메모리 컨트롤러" 하위 항목으로, VRAM 의 사용/여유는 used·total 차이로 구한다.
 */
async function refreshViaNvidiaSmi() {
  const stdout = await execFileP('nvidia-smi', [
    '--query-gpu=utilization.gpu,utilization.memory,memory.used,memory.total',
    '--format=csv,noheader,nounits',
  ]);
  const firstLine = stdout.trim().split('\n')[0];
  const [util, memUtil, used, total] = firstLine.split(',').map((s) => parseFloat(s.trim()));
  if (![util, memUtil, used, total].every(Number.isFinite) || total <= 0) throw new Error('nvidia-smi 응답 파싱 실패');
  cachedGpu = {
    percent: Math.round(util), available: true,
    breakdown: [
      { label: '코어', pct: Math.round(util) },
      { label: '메모리 컨트롤러', pct: Math.round(memUtil) },
    ],
  };
  const usedGb = +(used / 1024).toFixed(1);
  const totalGb = +(total / 1024).toFixed(1);
  cachedVram = {
    percent: Math.round((used / total) * 100),
    usedGb, totalGb, available: true,
    breakdown: [
      { label: '사용 중', gb: usedGb },
      { label: '여유', gb: +(totalGb - usedGb).toFixed(1) },
    ],
  };
}

/**
 * Windows: nvidia-smi 가 없을 때(AMD/Intel 전용 GPU) 벤더 무관 폴백 —
 * Windows 자체 GPU 성능 카운터로 사용률만 얻는다(VRAM 총량은 멀티 GPU 상황에서
 * 특정 어댑터로 확정하기 애매해 여기서는 시도하지 않음 — 애매한 값보단 N/A 유지).
 */
async function refreshViaWindowsCounters() {
  const script =
    "$ErrorActionPreference='Stop'; " +
    "$eng = Get-Counter '\\GPU Engine(*)\\Utilization Percentage'; " +
    '$max = ($eng.CounterSamples | Measure-Object -Property CookedValue -Maximum).Maximum; ' +
    "[PSCustomObject]@{ util = $max } | ConvertTo-Json -Compress";
  const stdout = await execFileP('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', script]);
  const parsed = JSON.parse(stdout.trim());
  const util = Number(parsed.util);
  if (!Number.isFinite(util)) throw new Error('GPU Engine 카운터 파싱 실패');
  cachedGpu = { percent: Math.max(0, Math.min(100, Math.round(util))), available: true };
  // VRAM: 신뢰 가능한 벤더-무관 총량 조회 방법이 없어 available:false 유지(가짜 수치 금지).
}

async function refreshHeavy() {
  if (process.platform === 'darwin') {
    // macOS GPU/VRAM 실측(powermetrics)은 관리자 권한이 필요해, 백그라운드 폴링에서
    // 조용히 암호를 요구하는 UX 변경을 임의로 넣지 않는다(사용자 확인 없이는 보류) —
    // RAM 세부 분해만 갱신.
    await refreshMacRam().catch(() => { cachedMacRamBreakdown = null; });
    return;
  }
  if (process.platform === 'win32') {
    try {
      await refreshViaNvidiaSmi();
    } catch {
      try {
        await refreshViaWindowsCounters();
      } catch {
        cachedGpu = { percent: null, available: false };
        cachedVram = { percent: null, available: false };
      }
    }
    return;
  }
  // linux 등: 표준 접근 없음 → 그대로 unavailable.
}

function maybeKickHeavyRefresh() {
  const now = Date.now();
  if (heavyRefreshing || now - lastHeavyRefresh < HEAVY_REFRESH_MS) return;
  heavyRefreshing = true;
  refreshHeavy()
    .catch(() => {})
    .finally(() => {
      heavyRefreshing = false;
      lastHeavyRefresh = Date.now();
    });
}

function sample() {
  maybeKickHeavyRefresh(); // fire-and-forget — 메인 프로세스를 블로킹하지 않음
  const cpu = cpuPercent();
  const ram = ramPercent();
  return {
    cpu: {
      percent: cpu.percent, available: true,
      breakdown: [
        { label: '시스템', pct: cpu.systemPct },
        { label: '사용자', pct: cpu.userPct },
        { label: '대기', pct: cpu.idlePct },
      ],
    },
    ram: {
      percent: ram.percent, usedGb: ram.usedGb, totalGb: ram.totalGb, available: true,
      breakdown: ramBreakdown(ram),
    },
    gpu: cachedGpu,
    vram: cachedVram,
  };
}

module.exports = { sample };
