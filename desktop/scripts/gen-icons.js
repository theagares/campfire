'use strict';
/**
 * scripts/gen-icons.js
 * 외부 에셋 없이 앱/트레이 아이콘 PNG 를 코드로 생성한다(빌드 재현성·CDN 금지).
 * UpSecurity 시그니처: Blue500(#3182F6) 라운드 스퀘어 + 흰 방패/체크.
 *   - build/icon.png   512x512 (electron-builder 가 win/mac 포맷으로 자동 변환)
 *   - assets/tray-icon.png 32x32 (트레이)
 */

const fs = require('fs');
const path = require('path');
const zlib = require('zlib');

function makePng(size, draw) {
  const px = Buffer.alloc(size * size * 4); // RGBA
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const [r, g, b, a] = draw(x, y, size);
      const i = (y * size + x) * 4;
      px[i] = r; px[i + 1] = g; px[i + 2] = b; px[i + 3] = a;
    }
  }
  // 스캔라인마다 필터 바이트(0) 추가
  const raw = Buffer.alloc(size * (size * 4 + 1));
  for (let y = 0; y < size; y++) {
    raw[y * (size * 4 + 1)] = 0;
    px.copy(raw, y * (size * 4 + 1) + 1, y * size * 4, (y + 1) * size * 4);
  }
  const idat = zlib.deflateSync(raw, { level: 9 });

  const chunk = (type, data) => {
    const len = Buffer.alloc(4);
    len.writeUInt32BE(data.length, 0);
    const typeBuf = Buffer.from(type, 'ascii');
    const crc = Buffer.alloc(4);
    crc.writeUInt32BE(crc32(Buffer.concat([typeBuf, data])) >>> 0, 0);
    return Buffer.concat([len, typeBuf, data, crc]);
  };
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(size, 0);
  ihdr.writeUInt32BE(size, 4);
  ihdr[8] = 8;   // bit depth
  ihdr[9] = 6;   // color type RGBA
  const sig = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);
  return Buffer.concat([sig, chunk('IHDR', ihdr), chunk('IDAT', idat), chunk('IEND', Buffer.alloc(0))]);
}

// CRC32
const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c >>> 0;
  }
  return t;
})();
function crc32(buf) {
  let c = 0xffffffff;
  for (let i = 0; i < buf.length; i++) c = CRC_TABLE[(c ^ buf[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

// 거리(점→선분)
function distToSeg(px, py, ax, ay, bx, by) {
  const dx = bx - ax, dy = by - ay;
  const len2 = dx * dx + dy * dy || 1;
  let t = ((px - ax) * dx + (py - ay) * dy) / len2;
  t = Math.max(0, Math.min(1, t));
  const cx = ax + t * dx, cy = ay + t * dy;
  return Math.hypot(px - cx, py - cy);
}

const BLUE = [49, 130, 246];

function drawIcon(x, y, size) {
  const s = size;
  const r = s * 0.22; // 코너 라운드
  // 라운드 스퀘어 마스크
  const inX = Math.min(Math.max(x, r), s - r);
  const inY = Math.min(Math.max(y, r), s - r);
  const dCorner = Math.hypot(x - inX, y - inY);
  const inside = x >= 0 && x < s && y >= 0 && y < s && dCorner <= r + 0.5;
  if (!inside) return [0, 0, 0, 0];

  // 체크마크 (흰색)
  const th = s * 0.075;
  const d1 = distToSeg(x, y, s * 0.30, s * 0.52, s * 0.44, s * 0.66);
  const d2 = distToSeg(x, y, s * 0.44, s * 0.66, s * 0.72, s * 0.36);
  if (Math.min(d1, d2) <= th) return [255, 255, 255, 255];

  return [BLUE[0], BLUE[1], BLUE[2], 255];
}

function write(rel, buf) {
  const p = path.resolve(__dirname, '..', rel);
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, buf);
  console.log('wrote', rel, `(${buf.length} bytes)`);
}

write('build/icon.png', makePng(512, drawIcon));
write('assets/tray-icon.png', makePng(32, drawIcon));
write('assets/icon.png', makePng(256, drawIcon));
console.log('done');
