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

const { parseVmStat, parseMacDisplays } = require('../main/system-metrics');

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
