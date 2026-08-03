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
let cachedMacRam = null; // { appBytes, wiredBytes, compressedBytes } | null

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

/**
 * RAM 사용량.
 *
 * macOS 에서 os.freemem() 을 그대로 쓰면 안 된다: 이 값은 vm_stat 의 "Pages free"
 * (완전히 비어 있는 페이지)만 센다. macOS 는 남는 메모리를 파일 캐시로 적극적으로
 * 채워두기 때문에 free 는 늘 몇백 MB 수준이고, total - free 로 구한 "사용 중"은
 * 활성 상태 보기(활성 모니터)의 "메모리 사용량"보다 훨씬 크게 나온다 — 앱과 실제
 * 화면이 다르다는 리포트의 원인이다.
 *
 * 활성 모니터의 "메모리 사용량" 정의를 그대로 따른다:
 *     앱 메모리(= Anonymous - Purgeable) + 와이어드 + 압축됨
 * 캐시된 파일(File-backed / Purgeable)은 필요하면 즉시 회수되므로 제외한다.
 *
 * vm_stat 을 아직 못 읽었거나(첫 tick) 실패하면 기존 방식으로 폴백한다 — 없는 것보다
 * 낫고, 8초 주기 갱신이 한 번 돌면 바로 정확해진다.
 */
function ramPercent() {
  const total = os.totalmem();
  if (total <= 0) return { percent: 0, usedGb: 0, freeGb: 0, totalGb: 0, source: 'none' };

  let used;
  let source;
  if (process.platform === 'darwin' && cachedMacRam) {
    used = cachedMacRam.appBytes + cachedMacRam.wiredBytes + cachedMacRam.compressedBytes;
    source = 'vm_stat';
  } else {
    used = total - os.freemem();
    source = 'os.freemem';
  }
  used = Math.max(0, Math.min(total, used));
  const free = total - used;
  return {
    percent: Math.round((used / total) * 100),
    usedGb: +(used / 1024 ** 3).toFixed(1),
    freeGb: +(free / 1024 ** 3).toFixed(1),
    totalGb: +(total / 1024 ** 3).toFixed(1),
    source,
  };
}

/**
 * RAM 세부 분해는 OS 마다 실제로 존재하는 개념이 다르다(동적 분기):
 *  - macOS: Wired/Compressed 는 커널이 실제로 구분 관리하는 실측 카테고리 → vm_stat 로 읽는다.
 *  - Windows/기타: "와이어드/압축됨" 같은 OS 레벨 개념 자체가 없어 지어낼 수 없음 →
 *    실측 가능한 사용 중/여유로 대체(§PLAN 원칙: 허위 수치 금지).
 */
function ramBreakdown(ram) {
  if (process.platform === 'darwin' && cachedMacRam) {
    const gb = (b) => +(b / 1024 ** 3).toFixed(1);
    return [
      { label: '앱', gb: gb(cachedMacRam.appBytes) },
      { label: '와이어드', gb: gb(cachedMacRam.wiredBytes) },
      { label: '압축됨', gb: gb(cachedMacRam.compressedBytes) },
    ];
  }
  return [
    { label: '사용 중', gb: ram.usedGb },
    { label: '여유', gb: ram.freeGb },
  ];
}

/**
 * vm_stat 출력 → 활성 모니터와 같은 정의의 메모리 구성(바이트).
 * 파싱 실패 시 null — 호출부가 폴백을 결정한다.
 *
 * 순수 함수로 분리해 둔 이유: macOS 없이도 실제 vm_stat 출력을 고정 입력으로 넣어
 * 검증할 수 있어야 한다(tests/system-metrics.test.js).
 */
function parseVmStat(stdout) {
  if (!stdout) return null;
  const pageMatch = stdout.match(/page size of (\d+) bytes/);
  const pageSize = pageMatch ? parseInt(pageMatch[1], 10) : 4096;
  const pages = (label) => {
    const m = stdout.match(new RegExp(label.replace(/\s/g, '\\s+') + ':\\s+(\\d+)\\.'));
    return m ? parseInt(m[1], 10) : 0;
  };
  const wired = pages('Pages wired down');
  // macOS 버전에 따라 라벨이 다름("occupied by compressor" 구버전 / "stored in compressor" 신버전)
  const compressed = pages('Pages occupied by compressor') || pages('Pages stored in compressor');
  const anonymous = pages('Anonymous pages');
  const purgeable = pages('Pages purgeable');
  // 최소한 이 둘은 있어야 신뢰할 수 있다(형식이 예상과 다르면 폴백).
  if (!wired && !anonymous) return null;
  return {
    appBytes: Math.max(0, anonymous - purgeable) * pageSize,
    wiredBytes: wired * pageSize,
    compressedBytes: compressed * pageSize,
  };
}

/** macOS: vm_stat 실측 갱신. 실패 시 null(ramPercent/ramBreakdown 이 폴백 처리). */
async function refreshMacRam() {
  cachedMacRam = parseVmStat(await execFileP('vm_stat', []));
}

/**
 * system_profiler SPDisplaysDataType -json 파싱 → { model, vramGb, unified }.
 *
 * Apple Silicon 은 GPU 가 시스템 메모리를 그대로 공유하는 통합 메모리라 "전용 VRAM"
 * 이라는 값 자체가 없다(그래서 이 필드가 아예 안 나온다). Intel/외장 GPU 는
 * spdisplays_vram(예: "1536 MB") 또는 sppci_vram(예: "8 GB") 으로 총량이 나온다.
 *
 * 사용량(used)은 공개 API 로는 권한 없이 구할 수 없다 — 지어내지 않고 비워둔다.
 */
function parseMacDisplays(jsonText) {
  let data;
  try { data = JSON.parse(jsonText); } catch { return null; }
  const list = data?.SPDisplaysDataType;
  if (!Array.isArray(list) || !list.length) return null;
  const g = list[0];
  const model = g.sppci_model || g._name || null;
  const raw = g.spdisplays_vram || g.sppci_vram || g.spdisplays_vram_shared || null;
  let vramGb = null;
  if (typeof raw === 'string') {
    const m = raw.match(/([\d.]+)\s*(MB|GB)/i);
    if (m) {
      const n = parseFloat(m[1]);
      vramGb = /gb/i.test(m[2]) ? n : +(n / 1024).toFixed(1);
    }
  }
  return { model, vramGb, unified: vramGb == null };
}

/**
 * ioreg 의 IOAccelerator 노드 → GPU 사용률 + GPU 메모리(바이트).
 *
 * 왜 이게 필요한가: system_profiler 는 "전용 VRAM 총량"만 알려주고 Apple Silicon 은
 * 통합 메모리라 그 필드가 아예 없다. 그래서 예전 구현은 M 시리즈 맥에서 VRAM 을
 * 영원히 N/A 로 남겼다 — 이유 문구는 붙었지만 사용자가 원한 "숫자"는 끝내 안 나왔다.
 *
 * IOAccelerator 노드의 PerformanceStatistics 딕셔너리는 커널이 그대로 노출하는
 * 실측치이고 관리자 권한이 필요 없다(powermetrics 와 달리). 키 이름이 GPU 계열마다
 * 다르므로 아는 이름을 전부 훑는다:
 *   - Apple Silicon: "In use system memory" / "Alloc system memory"  (통합 메모리)
 *   - Intel/AMD:     "vramUsedBytes" / "vramFreeBytes"               (전용 VRAM)
 *   - 사용률 공통:    "Device Utilization %" (없으면 "Renderer Utilization %")
 * 단위가 확실치 않은 키("GPU Core Utilization" 등)는 일부러 안 쓴다 — 틀린 수치보단 N/A.
 *
 * 순수 함수라 macOS 없이도 실제 출력 고정 입력으로 검증할 수 있다.
 * 형식이 예상과 다르면 null → 호출부가 기존 system_profiler 경로로 폴백한다.
 */
function parseIoregAccelerator(stdout) {
  if (!stdout) return null;

  // PerformanceStatistics 값은 중첩 딕셔너리라 정규식 하나로는 끝을 못 찾는다.
  // 여는 중괄호부터 짝이 맞는 닫는 중괄호까지 세어서 잘라낸다.
  const blocks = [];
  let cursor = 0;
  for (;;) {
    const at = stdout.indexOf('"PerformanceStatistics"', cursor);
    if (at < 0) break;
    const start = stdout.indexOf('{', at);
    if (start < 0) break;
    let depth = 0;
    let end = -1;
    for (let i = start; i < stdout.length; i += 1) {
      if (stdout[i] === '{') depth += 1;
      else if (stdout[i] === '}') {
        depth -= 1;
        if (depth === 0) { end = i; break; }
      }
    }
    if (end < 0) break;
    const stats = {};
    const body = stdout.slice(start + 1, end);
    const pair = /"([^"]+)"\s*=\s*(-?\d+)/g;
    let m;
    while ((m = pair.exec(body))) stats[m[1]] = parseInt(m[2], 10);
    if (Object.keys(stats).length) blocks.push(stats);
    cursor = end + 1;
  }
  if (!blocks.length) return null;

  const num = (stats, keys) => {
    for (const k of keys) {
      if (Number.isFinite(stats[k])) return stats[k];
    }
    return null;
  };

  // 여러 GPU(내장+외장)가 잡히면 실제로 쓰이고 있는 쪽을 고른다: 전용 VRAM 을 보고하는
  // 어댑터를 우선하고, 그 다음은 GPU 메모리를 가장 많이 쓰는 쪽.
  let best = null;
  for (const stats of blocks) {
    const vramUsed = num(stats, ['vramUsedBytes']);
    const vramFree = num(stats, ['vramFreeBytes']);
    const inUse = num(stats, ['In use system memory', 'inUseSystemMemory']);
    const alloc = num(stats, ['Alloc system memory', 'allocSystemMemory']);
    const utilPct = num(stats, ['Device Utilization %', 'Renderer Utilization %']);
    const dedicated = vramUsed != null && vramFree != null
      ? { usedBytes: vramUsed, totalBytes: vramUsed + vramFree }
      : null;
    const unified = inUse != null ? { inUseBytes: inUse, allocBytes: alloc ?? inUse } : null;
    if (!dedicated && !unified && utilPct == null) continue;
    const rank = (dedicated ? 1e18 : 0) + (dedicated?.usedBytes ?? unified?.inUseBytes ?? 0);
    if (!best || rank > best.rank) best = { rank, utilPct, dedicated, unified };
  }
  if (!best) return null;
  return { utilPct: best.utilPct, dedicated: best.dedicated, unified: best.unified };
}

/**
 * macOS GPU/VRAM.
 *
 * 1순위는 ioreg(위 파서) — 권한 없이 사용률과 GPU 메모리 실측치를 다 준다.
 * ioreg 가 아무것도 못 주면 예전 경로(system_profiler)로 폴백해 최소한 전용 VRAM
 * 총량이나 "왜 못 읽는지"라도 보여준다.
 *
 * 사용률에 powermetrics 는 여전히 안 쓴다 — 관리자 권한을 요구해서, 백그라운드 폴링이
 * 조용히 암호를 묻는 UX 가 되기 때문이다.
 */
async function refreshMacGpu() {
  const model = await macGpuModel();

  // GPU 노드의 클래스 이름이 세대마다 다르다(Intel 계열은 IOAccelerator, Apple Silicon 은
  // AGXAccelerator/IOGPU). ioreg -c 가 하위 클래스까지 잡아주긴 하지만 상속 관계를
  // 가정하지 않고 후보를 순서대로 시도한다 — 하나라도 걸리면 끝낸다.
  let accel = null;
  for (const cls of ['IOAccelerator', 'AGXAccelerator', 'IOGPU']) {
    try {
      // -w 0: 줄 너비 제한 해제. 이게 없으면 PerformanceStatistics 가 중간에서 잘려
      // 필요한 키가 통째로 사라진다. -d 1: 매칭된 노드 자신만(자식 제외).
      accel = parseIoregAccelerator(await execFileP('ioreg', ['-r', '-d', '1', '-w', '0', '-c', cls]));
    } catch {
      accel = null;
    }
    if (accel) break;
  }

  if (accel && accel.utilPct != null) {
    const pct = Math.max(0, Math.min(100, Math.round(accel.utilPct)));
    cachedGpu = { percent: pct, available: true, reason: model || null };
  } else {
    cachedGpu = {
      percent: null,
      available: false,
      reason: model ? `${model} — 사용률을 읽지 못함` : 'GPU 사용률을 읽지 못함',
    };
  }

  const gb = (b) => +(b / 1024 ** 3).toFixed(1);

  // 전용 VRAM 이 있는 맥(Intel/AMD): 그대로 사용/총량.
  if (accel && accel.dedicated && accel.dedicated.totalBytes > 0) {
    const { usedBytes, totalBytes } = accel.dedicated;
    cachedVram = {
      percent: Math.round((usedBytes / totalBytes) * 100),
      usedGb: gb(usedBytes), totalGb: gb(totalBytes), available: true,
      reason: model || null,
      breakdown: [
        { label: '사용 중', gb: gb(usedBytes) },
        { label: '여유', gb: gb(totalBytes - usedBytes) },
      ],
    };
    return;
  }

  // Apple Silicon: 전용 VRAM 이라는 물건이 없고 GPU 가 시스템 메모리를 그대로 쓴다.
  // 그래서 "GPU 가 실제로 쓰고 있는 메모리 / 전체 시스템 메모리"로 보여준다 —
  // 지어낸 수치가 아니라 커널이 보고하는 실측 점유량이다.
  const totalBytes = os.totalmem();
  if (accel && accel.unified && totalBytes > 0) {
    const { inUseBytes, allocBytes } = accel.unified;
    cachedVram = {
      percent: Math.max(0, Math.min(100, Math.round((inUseBytes / totalBytes) * 100))),
      usedGb: gb(inUseBytes), totalGb: gb(totalBytes), available: true,
      reason: model ? `${model} — 통합 메모리` : '통합 메모리',
      breakdown: [
        { label: '사용 중', gb: gb(inUseBytes) },
        { label: '할당됨', gb: gb(allocBytes) },
      ],
    };
    return;
  }

  // ioreg 가 실패한 경우에만 오는 폴백 — 총량만이라도.
  const info = await macDisplays();
  if (info && info.vramGb != null) {
    cachedVram = {
      percent: null, usedGb: null, totalGb: info.vramGb, available: true,
      reason: model || null,
    };
  } else {
    cachedVram = {
      percent: null, available: false,
      reason: info?.unified ? '통합 메모리 — GPU 메모리를 읽지 못함' : 'VRAM 정보를 읽지 못함',
    };
  }
}

/** system_profiler 결과 캐시 — GPU 모델명은 잘 안 바뀌는데 조회는 느리다(수백 ms~수 초). */
let cachedDisplays;
async function macDisplays() {
  if (cachedDisplays === undefined) {
    try {
      cachedDisplays = parseMacDisplays(
        await execFileP('system_profiler', ['SPDisplaysDataType', '-json'], { timeout: 8000 }),
      );
    } catch {
      cachedDisplays = null;
    }
  }
  return cachedDisplays;
}

async function macGpuModel() {
  const info = await macDisplays();
  return info?.model || null;
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
    await refreshMacRam().catch(() => { cachedMacRam = null; });
    await refreshMacGpu().catch(() => {
      cachedGpu = { percent: null, available: false };
      cachedVram = { percent: null, available: false };
    });
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
      // 어느 경로로 계산했는지(vm_stat | os.freemem) — macOS 에서 수치가 활성 모니터와
      // 다를 때 "vm_stat 을 실제로 읽고 있는지" 를 바로 구분하려고 남긴다.
      source: ram.source,
      breakdown: ramBreakdown(ram),
    },
    gpu: cachedGpu,
    vram: cachedVram,
  };
}

// 파서들은 macOS 없이도 실제 출력 고정 입력으로 검증하려고 노출한다.
module.exports = { sample, parseVmStat, parseMacDisplays, parseIoregAccelerator };
