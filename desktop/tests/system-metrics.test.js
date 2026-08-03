'use strict';
/**
 * system-metrics.js 의 macOS 파서 테스트 (node --test, 의존성 없음).
 *
 * 왜 파서를 따로 떼서 테스트하나: 이 코드는 macOS 에서만 도는데 개발/CI 는 그렇지
 * 않을 수 있다. 실제 vm_stat / system_profiler 출력을 고정 입력으로 넣어 계산식만은
 * 어디서든 검증한다.
 *
 * 배경(리포트): 앱이 보여주는 RAM 이 활성 모니터와 달랐다. os.freemem() 은 vm_stat 의
 * "Pages free"(완전히 빈 페이지)만 세는데, macOS 는 남는 메모리를 파일 캐시로 채워두므로
 * free 는 늘 작고 total-free 는 실제 사용량보다 훨씬 크게 나온다. 활성 모니터의
 * "메모리 사용량" = 앱(Anonymous-Purgeable) + 와이어드 + 압축됨 정의를 따라야 한다.
 */

const test = require('node:test');
const assert = require('node:assert');

const { parseVmStat, parseMacDisplays, parseIoregAccelerator } = require('../main/system-metrics');

// Apple Silicon(page size 16384) 실제 vm_stat 형식.
const VM_STAT = `Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                52341.
Pages active:                             412345.
Pages inactive:                           398765.
Pages speculative:                         12345.
Pages throttled:                               0.
Pages wired down:                         123456.
Pages purgeable:                           20000.
"Translation faults":                  987654321.
Pages copy-on-write:                     1234567.
Pages zero filled:                     123456789.
Pages reactivated:                        234567.
Pages purged:                             345678.
File-backed pages:                        250000.
Anonymous pages:                          573455.
Pages stored in compressor:               180000.
Pages occupied by compressor:              45678.
Swapins:                                       0.
Swapouts:                                      0.`;

const PAGE = 16384;
const GB = 1024 ** 3;

test('vm_stat: 앱 = Anonymous - Purgeable, 와이어드/압축됨은 그대로', () => {
  const r = parseVmStat(VM_STAT);
  assert.ok(r, '파싱에 성공해야 한다');
  assert.equal(r.appBytes, (573455 - 20000) * PAGE);
  assert.equal(r.wiredBytes, 123456 * PAGE);
  // "occupied by compressor"(실제 압축 점유)를 쓴다 — "stored in compressor"(압축 전 원본
  // 크기)가 아니다. 활성 모니터의 "압축됨"과 같은 값이어야 한다.
  assert.equal(r.compressedBytes, 45678 * PAGE);
});

test('vm_stat: 사용량이 free 기반 계산보다 작아야 한다(캐시 제외)', () => {
  const r = parseVmStat(VM_STAT);
  const used = r.appBytes + r.wiredBytes + r.compressedBytes;

  // 같은 스냅샷을 os.freemem() 방식으로 계산하면(= total - free) 얼마나 부풀려지는지.
  const totalPages = 52341 + 412345 + 398765 + 12345 + 123456 + 45678;
  const total = totalPages * PAGE;
  const freeBased = total - 52341 * PAGE;

  assert.ok(used < freeBased, '캐시를 제외했으니 free 기반보다 작아야 한다');
  // 대략 10.9GB vs 15.6GB — 사용자가 본 "앱과 활성 모니터가 다름"의 크기다.
  assert.ok(used / GB > 10 && used / GB < 12, `예상 범위를 벗어남: ${(used / GB).toFixed(1)}GB`);
});

test('vm_stat: 구버전 라벨(stored in compressor 만 있는 경우)도 읽는다', () => {
  const old = VM_STAT.split('\n').filter((l) => !l.includes('occupied by compressor')).join('\n');
  const r = parseVmStat(old);
  assert.equal(r.compressedBytes, 180000 * PAGE);
});

test('vm_stat: 형식이 다르면 null 을 돌려 폴백하게 한다', () => {
  assert.equal(parseVmStat(''), null);
  assert.equal(parseVmStat('전혀 다른 출력'), null);
});

test('system_profiler: Apple Silicon 은 전용 VRAM 이 없다(unified)', () => {
  const json = JSON.stringify({
    SPDisplaysDataType: [{
      _name: 'Apple M2 Pro',
      sppci_model: 'Apple M2 Pro',
      spdisplays_mtlgpufamilysupport: 'spdisplays_metal3',
    }],
  });
  const r = parseMacDisplays(json);
  assert.equal(r.model, 'Apple M2 Pro');
  assert.equal(r.vramGb, null);
  assert.equal(r.unified, true);
});

test('system_profiler: 외장 GPU 는 VRAM 총량을 읽는다(GB 표기)', () => {
  const json = JSON.stringify({
    SPDisplaysDataType: [{ _name: 'Radeon Pro 5500M', sppci_model: 'AMD Radeon Pro 5500M', spdisplays_vram: '8 GB' }],
  });
  const r = parseMacDisplays(json);
  assert.equal(r.vramGb, 8);
  assert.equal(r.unified, false);
  assert.equal(r.model, 'AMD Radeon Pro 5500M');
});

test('system_profiler: 내장 GPU 의 MB 표기도 GB 로 환산한다', () => {
  const json = JSON.stringify({
    SPDisplaysDataType: [{ _name: 'Intel UHD Graphics 630', spdisplays_vram_shared: '1536 MB' }],
  });
  const r = parseMacDisplays(json);
  assert.equal(r.vramGb, 1.5);
});

test('system_profiler: 깨진 출력은 null', () => {
  assert.equal(parseMacDisplays('not json'), null);
  assert.equal(parseMacDisplays(JSON.stringify({ SPDisplaysDataType: [] })), null);
});

// ── ioreg IOAccelerator ────────────────────────────────────────────────────
// 배경(리포트): "맥에서 VRAM 이 아직도 안 보임". Apple Silicon 은 통합 메모리라
// system_profiler 에 VRAM 필드가 없고, 그래서 예전 구현은 영원히 N/A 였다. ioreg 의
// PerformanceStatistics 는 관리자 권한 없이 실제 GPU 메모리/사용률을 준다.

// Apple Silicon 실제 형식(중첩 딕셔너리 + 무관한 키 다수).
const IOREG_APPLE = `+-o IOGPU  <class AGXAcceleratorG13X, id 0x100000452, registered, matched, active, busy 0 (0 ms), retain 32>
  {
    "IOClass" = "AGXAcceleratorG13X"
    "IOPowerManagement" = {"CurrentPowerState"=1,"MaxPowerState"=1}
    "PerformanceStatistics" = {"Alloc system memory"=2371584000,"In use system memory"=1099497472,"Device Utilization %"=37,"Renderer Utilization %"=21,"Tiler Utilization %"=4,"recoveryCount"=0,"SplitSceneCount"=0}
    "IOAccelRevision" = 2
  }`;

const GB_B = 1024 ** 3;

test('ioreg: Apple Silicon 은 통합 메모리 점유량을 준다', () => {
  const r = parseIoregAccelerator(IOREG_APPLE);
  assert.ok(r, '파싱에 성공해야 한다');
  assert.equal(r.utilPct, 37);
  assert.equal(r.dedicated, null, '전용 VRAM 은 없어야 한다');
  assert.equal(r.unified.inUseBytes, 1099497472);
  assert.equal(r.unified.allocBytes, 2371584000);
  // 이게 핵심 회귀 방지: 예전엔 여기서 아무 수치도 못 얻어 VRAM 이 N/A 였다.
  assert.ok(r.unified.inUseBytes / GB_B > 1, 'GB 단위로 의미 있는 값이어야 한다');
});

test('ioreg: 중첩 딕셔너리를 만나도 블록 끝을 정확히 찾는다', () => {
  // PerformanceStatistics 안에 또 딕셔너리가 들어간 형태 — 정규식 하나로 자르면
  // 첫 '}' 에서 끊겨 뒤쪽 키를 통째로 놓친다.
  const nested = IOREG_APPLE.replace(
    '"recoveryCount"=0',
    '"nested"={"a"=1,"b"=2},"recoveryCount"=0',
  );
  const r = parseIoregAccelerator(nested);
  assert.equal(r.unified.inUseBytes, 1099497472);
  assert.equal(r.utilPct, 37);
});

test('ioreg: Intel/AMD 는 전용 VRAM 사용/총량을 준다', () => {
  const amd = `+-o IOGPU  <class AMDRadeonX6000, id 0x1000004a1>
  {
    "PerformanceStatistics" = {"vramFreeBytes"=6207545344,"vramUsedBytes"=1385439232,"Device Utilization %"=12,"hardwareWaitTime"=0}
  }`;
  const r = parseIoregAccelerator(amd);
  assert.equal(r.utilPct, 12);
  assert.equal(r.unified, null);
  assert.equal(r.dedicated.usedBytes, 1385439232);
  assert.equal(r.dedicated.totalBytes, 1385439232 + 6207545344);
});

test('ioreg: 내장+외장이 같이 잡히면 전용 VRAM 쪽을 고른다', () => {
  const both = `+-o IOGPU  <class AppleIntelKBLGraphics>
  {
    "PerformanceStatistics" = {"In use system memory"=50000000,"Device Utilization %"=2}
  }
+-o IOGPU  <class AMDRadeonX6000>
  {
    "PerformanceStatistics" = {"vramFreeBytes"=6000000000,"vramUsedBytes"=2000000000,"Device Utilization %"=88}
  }`;
  const r = parseIoregAccelerator(both);
  assert.equal(r.utilPct, 88, '실제로 쓰이는 외장 GPU 의 사용률이어야 한다');
  assert.equal(r.dedicated.usedBytes, 2000000000);
});

test('ioreg: 사용률 키가 없어도 메모리만은 살린다', () => {
  const noUtil = IOREG_APPLE
    .replace('"Device Utilization %"=37,', '')
    .replace('"Renderer Utilization %"=21,', '');
  const r = parseIoregAccelerator(noUtil);
  assert.equal(r.utilPct, null);
  assert.equal(r.unified.inUseBytes, 1099497472);
});

test('ioreg: 단위가 불확실한 키는 사용률로 쓰지 않는다', () => {
  // "GPU Core Utilization" 은 기기에 따라 % 가 아닌 스케일로 나온다 — 틀린 수치보단 N/A.
  const odd = `+-o IOGPU
  {
    "PerformanceStatistics" = {"GPU Core Utilization"=8500000,"In use system memory"=1000}
  }`;
  const r = parseIoregAccelerator(odd);
  assert.equal(r.utilPct, null);
});

// 실제 Apple Silicon MacBook Pro 출력 그대로(2026-08, 리포터 기기). 키 순서와
// 이름을 손대지 않았다 — 이 문자열이 고쳐야 할 진짜 입력이다.
const IOREG_REAL = `"PerformanceStatistics" = {"In use system memory (driver)"=0,"Alloc system memory"=7996702720,"Tiler Utilization %"=9,"recoveryCount"=0,"lastRecoveryTime"=0,"Renderer Utilization %"=16,"TiledSceneBytes"=819200,"Device Utilization %"=16,"SplitSceneCount"=0,"Allocated PB Size"=91750400,"In use system memory"=1569652736}`;

test('ioreg: 실기 출력 — "(driver)" 미끼 키에 속지 않는다', () => {
  const r = parseIoregAccelerator(IOREG_REAL);
  assert.ok(r, '실기 출력은 반드시 파싱돼야 한다');
  // "In use system memory (driver)"=0 이 진짜 키보다 먼저 나온다. 부분 문자열로
  // 찾으면 0 을 집어서 VRAM 이 0GB 로 표시된다 — 정확히 일치하는 키만 써야 한다.
  assert.equal(r.unified.inUseBytes, 1569652736);
  assert.notEqual(r.unified.inUseBytes, 0);
  assert.equal(r.unified.allocBytes, 7996702720);
  assert.equal(r.utilPct, 16, 'Tiler(9)/Renderer(16) 가 아니라 Device 사용률이어야 한다');
  assert.equal(r.dedicated, null, 'Apple Silicon 은 전용 VRAM 이 없다');
  // 화면에 실제로 뜰 값: 약 1.5GB 사용 / 7.4GB 할당.
  assert.equal(+(r.unified.inUseBytes / GB_B).toFixed(1), 1.5);
  assert.equal(+(r.unified.allocBytes / GB_B).toFixed(1), 7.4);
});

test('ioreg: 형식이 다르면 null 을 돌려 폴백하게 한다', () => {
  assert.equal(parseIoregAccelerator(''), null);
  assert.equal(parseIoregAccelerator('전혀 다른 출력'), null);
  // 키는 있는데 값이 하나도 안 잡히는 경우(-w 0 을 빼먹어 잘린 출력 등).
  assert.equal(parseIoregAccelerator('"PerformanceStatistics" = {}'), null);
});
