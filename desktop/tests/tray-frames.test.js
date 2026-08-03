'use strict';
/**
 * 트레이 불꽃 애니메이션 에셋 무결성 (node --test, 의존성 없음).
 *
 * 왜 필요한가: main/tray.js 는 프레임이 한 장이라도 깨지면 애니메이션을 아예 켜지 않고
 * 정지 아이콘으로 조용히 물러난다(그게 맞다 — 메뉴바에서 깜빡이다 마는 것보다 낫다).
 * 대신 그래서 파일이 하나 빠져도 아무도 모른다. 그 계약을 여기서 잡는다.
 *
 * PNG 는 디코드하지 않고 헤더(IHDR)만 읽는다. 실제로 틀리기 쉬운 건 색이 아니라
 * "크기가 다른 그림을 넣었다" 쪽이고, 그건 헤더만으로 잡힌다.
 */

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const FRAMES_DIR = path.join(__dirname, '..', 'assets', 'tray-frames');
const MANIFEST = path.join(FRAMES_DIR, 'manifest.json');

/** IHDR 만 읽는다 — 폭/높이/비트심도/컬러타입. */
function pngHeader(file) {
  const b = fs.readFileSync(file);
  const sig = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  assert.ok(b.subarray(0, 8).equals(sig), `${path.basename(file)} 는 PNG 가 아니다`);
  return {
    width: b.readUInt32BE(16), height: b.readUInt32BE(20),
    bitDepth: b[24], colorType: b[25],
  };
}

test('manifest 가 있고 재생 간격이 유효하다', () => {
  const m = JSON.parse(fs.readFileSync(MANIFEST, 'utf-8'));
  assert.ok(Number.isFinite(m.frameIntervalMs) && m.frameIntervalMs > 0,
    'frameIntervalMs 가 없으면 tray.js 가 기본값으로 돌아가 의도한 속도가 안 나온다');
  assert.ok(m.mac && Array.isArray(m.mac.idle) && m.mac.idle.length > 0,
    'mac.idle 은 필수 — 이게 없으면 애니메이션이 통째로 꺼진다');
});

test('평상시(idle)와 검사 중(busy) 세트가 모두 있고 길이가 같다', () => {
  const m = JSON.parse(fs.readFileSync(MANIFEST, 'utf-8'));
  assert.ok(Array.isArray(m.mac.busy) && m.mac.busy.length > 0,
    'busy 가 없으면 검사 중에도 불꽃 세기가 안 변한다(기능이 조용히 사라진다)');
  assert.equal(m.mac.busy.length, m.mac.idle.length,
    '세트 길이가 다르면 상태 전환 때 프레임 위상이 튄다');
});

test('모든 프레임 파일이 @2x 짝과 함께 존재한다', () => {
  const m = JSON.parse(fs.readFileSync(MANIFEST, 'utf-8'));
  for (const state of ['idle', 'busy']) {
    for (const name of m.mac[state]) {
      const one = path.join(FRAMES_DIR, name);
      const two = path.join(FRAMES_DIR, name.replace(/\.png$/, '@2x.png'));
      assert.ok(fs.existsSync(one), `${name} 없음`);
      // @2x 는 nativeImage 가 파일명 규칙으로 알아서 집어간다 — 없으면 레티나에서 뭉갠다.
      assert.ok(fs.existsSync(two), `${path.basename(two)} 없음 (레티나용)`);
    }
  }
});

test('프레임 규격이 macOS 메뉴바 크기(22 / 44)와 PNG-32 를 지킨다', () => {
  const m = JSON.parse(fs.readFileSync(MANIFEST, 'utf-8'));
  for (const state of ['idle', 'busy']) {
    for (const name of m.mac[state]) {
      const a = pngHeader(path.join(FRAMES_DIR, name));
      assert.deepEqual([a.width, a.height], [22, 22], `${name} 는 22x22 여야 한다`);
      assert.equal(a.colorType, 6, `${name} 는 RGBA(컬러타입 6)여야 한다 — 알파가 곧 마스크다`);
      assert.equal(a.bitDepth, 8, `${name} 는 8bit 이어야 한다`);

      const two = name.replace(/\.png$/, '@2x.png');
      const b = pngHeader(path.join(FRAMES_DIR, two));
      assert.deepEqual([b.width, b.height], [44, 44], `${two} 는 44x44 여야 한다`);
      assert.equal(b.colorType, 6, `${two} 는 RGBA 여야 한다`);
    }
  }
});

test('manifest 에 안 적힌 프레임 파일이 굴러다니지 않는다', () => {
  // 세트를 갈아끼우면서 옛 프레임을 안 지우면 설치본에 죽은 파일이 실려 나간다.
  const m = JSON.parse(fs.readFileSync(MANIFEST, 'utf-8'));
  const listed = new Set(['manifest.json']);
  for (const state of ['idle', 'busy']) {
    for (const name of m.mac[state]) {
      listed.add(name);
      listed.add(name.replace(/\.png$/, '@2x.png'));
    }
  }
  const stray = fs.readdirSync(FRAMES_DIR).filter((f) => !listed.has(f));
  assert.deepEqual(stray, [], `manifest 에 없는 파일: ${stray.join(', ')}`);
});
