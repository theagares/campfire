'use strict';
/**
 * scripts/gen-icons.js
 * 손으로 그린 마스터 이미지에서 앱·트레이·확장 아이콘을 전부 생성한다.
 *
 *   node scripts/gen-icons.js
 *
 * 외부 라이브러리를 쓰지 않는다(이 저장소 관례 — 바이너리/CDN 의존 금지). PNG 디코드·
 * 리사이즈·인코드와 ICO 컨테이너를 직접 처리한다. 축소는 area-average(box) 로 하는데,
 * 큰 마스터에서 작은 아이콘으로 내리는 이 용도에는 bilinear 보다 이쪽이 정확하다.
 * 계산은 선형 광량(linear light) + 알파 프리멀티플라이 상태에서 한다 — sRGB 값을 그대로
 * 평균 내면 경계가 어두워지고, 프리멀티플라이를 빼먹으면 투명한 가장자리에 검은
 * 테두리(halo)가 생긴다.
 *
 * ── 입력: assets/source/ (손으로 그리는 것) ─────────────────────────────────
 *   app-icon.png   1024x1024 이상, 정사각, RGBA.
 *                  macOS 규칙상 실제 아트는 캔버스의 약 80%(1024 기준 824px)만 차지하고
 *                  나머지는 투명 여백이어야 Dock 에서 다른 앱과 크기가 맞는다. 모서리
 *                  둥근 처리도 그림에 포함해야 한다(OS 가 안 깎아준다).
 *   tray-mac.png   88x88 이상, 정사각. **단색 검정 + 투명 배경**.
 *                  macOS 템플릿 이미지는 알파만 읽고 RGB 를 버리므로 색은 무의미하다.
 *   tray-win.png   64x64 이상, 정사각. Windows 는 템플릿 반전을 안 해주므로 다크
 *                  작업표시줄에서 보이도록 흰색/컬러로 그린다.
 *   ui-hero.png    224x224 이상, 정사각, RGBA. (선택)
 *                  앱 홈 화면과 확장 사이드바에 크게 들어가는 일러스트. 아이콘 마크와
 *                  달리 16px 까지 줄어들 일이 없으므로 디테일이 있어도 된다.
 *                  없으면 아이콘 마크에서 대신 뽑는다.
 *
 * ── 애니메이션 ──────────────────────────────────────────────────────────────
 * 앱 아이콘(Dock/작업표시줄)은 macOS·Windows 모두 애니메이션을 지원하지 않는다 —
 * app-icon 은 항상 정지 이미지 하나다. 애니메이션이 가능한 건 트레이뿐이고, 프레임을
 * 갈아끼우는 방식이다(tray.setImage). 그래서 트레이 마스터는 파일 대신 폴더로 줄 수 있다:
 *
 *   assets/source/tray-mac/frame-01.png, frame-02.png, ...   (파일명 정렬 순서대로 재생)
 *   assets/source/tray-win/frame-01.png, ...
 *
 * 폴더가 있으면 프레임별 크기 세트를 만들고 assets/tray-frames/manifest.json 에 목록을
 * 적어둔다. 정지 상태로 쓸 대표 이미지(tray-icon.png)는 1번 프레임이다. 파일과 폴더가
 * 둘 다 있으면 폴더를 쓴다.
 */

const fs = require('fs');
const path = require('path');
const zlib = require('zlib');

const DESKTOP = path.join(__dirname, '..');
const REPO = path.join(DESKTOP, '..');
const SRC = path.join(DESKTOP, 'assets', 'source');

// 트레이 프레임 재생 간격(ms). manifest 에 실어 런타임이 하드코딩하지 않게 한다.
const FRAME_INTERVAL_MS = 100;

// ─────────────────────────────────────────────────────────────────────────────
// PNG 디코드
// ─────────────────────────────────────────────────────────────────────────────
const PNG_SIG = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

function decodePng(buf) {
  if (buf.length < 8 || !buf.subarray(0, 8).equals(PNG_SIG)) throw new Error('PNG 파일이 아닙니다');

  let off = 8;
  let ihdr = null, palette = null, trns = null;
  const idat = [];

  while (off + 8 <= buf.length) {
    const len = buf.readUInt32BE(off);
    const type = buf.toString('ascii', off + 4, off + 8);
    const data = buf.subarray(off + 8, off + 8 + len);
    off += 12 + len; // len(4) + type(4) + data + crc(4)

    if (type === 'IHDR') {
      ihdr = {
        width: data.readUInt32BE(0), height: data.readUInt32BE(4),
        bitDepth: data[8], colorType: data[9], interlace: data[12],
      };
    } else if (type === 'PLTE') palette = Buffer.from(data);
    else if (type === 'tRNS') trns = Buffer.from(data);
    else if (type === 'IDAT') idat.push(Buffer.from(data));
    else if (type === 'IEND') break;
  }

  if (!ihdr) throw new Error('IHDR 청크가 없습니다');
  if (ihdr.interlace !== 0) throw new Error('인터레이스(Adam7) PNG 는 지원하지 않습니다 — 인터레이스 없이 다시 내보내주세요');

  const { width: w, height: h, bitDepth: bd, colorType: ct } = ihdr;
  const CHANNELS = { 0: 1, 2: 3, 3: 1, 4: 2, 6: 4 };
  const ch = CHANNELS[ct];
  if (ch === undefined) throw new Error(`지원하지 않는 컬러 타입: ${ct}`);
  const okDepth = ct === 3 ? [1, 2, 4, 8] : [8, 16];
  if (!okDepth.includes(bd)) {
    throw new Error(`지원하지 않는 비트 심도 ${bd}(컬러 타입 ${ct}) — PNG-32(RGBA 8bit)로 내보내주세요`);
  }
  if (ct === 3 && !palette) throw new Error('팔레트 PNG 인데 PLTE 청크가 없습니다');

  const raw = zlib.inflateSync(Buffer.concat(idat));
  const bitsPerPx = ch * bd;
  const bytesPerRow = Math.ceil((bitsPerPx * w) / 8);
  const bpp = Math.max(1, Math.ceil(bitsPerPx / 8)); // 필터가 참조하는 바이트 간격
  if (raw.length < (bytesPerRow + 1) * h) throw new Error('PNG 데이터가 잘렸습니다');

  // 스캔라인 필터 해제
  const lines = Buffer.alloc(bytesPerRow * h);
  let prev = Buffer.alloc(bytesPerRow);
  for (let y = 0; y < h; y++) {
    const ft = raw[y * (bytesPerRow + 1)];
    const cur = Buffer.from(raw.subarray(y * (bytesPerRow + 1) + 1, (y + 1) * (bytesPerRow + 1)));
    for (let i = 0; i < bytesPerRow; i++) {
      const a = i >= bpp ? cur[i - bpp] : 0;
      const b = prev[i];
      const c = i >= bpp ? prev[i - bpp] : 0;
      let v = cur[i];
      if (ft === 1) v += a;
      else if (ft === 2) v += b;
      else if (ft === 3) v += (a + b) >> 1;
      else if (ft === 4) {
        const p = a + b - c;
        const pa = Math.abs(p - a), pb = Math.abs(p - b), pc = Math.abs(p - c);
        v += pa <= pb && pa <= pc ? a : pb <= pc ? b : c;
      } else if (ft !== 0) throw new Error(`알 수 없는 스캔라인 필터: ${ft}`);
      cur[i] = v & 0xff;
    }
    cur.copy(lines, y * bytesPerRow);
    prev = cur;
  }

  // RGBA8 로 펼치기 (16bit 은 상위 바이트만 취해 8bit 으로 축약)
  const out = Buffer.alloc(w * h * 4);
  const maxVal = (1 << bd) - 1;
  const scale = bd === 16 ? 1 : 255 / maxVal;
  const sample = (row, idx) => {
    if (bd === 16) return lines[row * bytesPerRow + idx * 2];
    if (bd === 8) return lines[row * bytesPerRow + idx];
    const bit = idx * bd;
    return (lines[row * bytesPerRow + (bit >> 3)] >> (8 - bd - (bit & 7))) & maxVal;
  };

  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const o = (y * w + x) * 4;
      if (ct === 3) {
        const i = sample(y, x);
        out[o] = palette[i * 3]; out[o + 1] = palette[i * 3 + 1]; out[o + 2] = palette[i * 3 + 2];
        out[o + 3] = trns && i < trns.length ? trns[i] : 255;
      } else if (ct === 0 || ct === 4) {
        const g = Math.round(sample(y, x * ch) * scale);
        out[o] = out[o + 1] = out[o + 2] = g;
        out[o + 3] = ct === 4 ? Math.round(sample(y, x * ch + 1) * scale) : 255;
      } else {
        out[o] = Math.round(sample(y, x * ch) * scale);
        out[o + 1] = Math.round(sample(y, x * ch + 1) * scale);
        out[o + 2] = Math.round(sample(y, x * ch + 2) * scale);
        out[o + 3] = ct === 6 ? Math.round(sample(y, x * ch + 3) * scale) : 255;
      }
    }
  }
  return { width: w, height: h, data: out };
}

// ─────────────────────────────────────────────────────────────────────────────
// 리사이즈 (area-average · 선형 광량 · 프리멀티플라이)
// ─────────────────────────────────────────────────────────────────────────────
const SRGB2LIN = new Float64Array(256);
for (let i = 0; i < 256; i++) {
  const c = i / 255;
  SRGB2LIN[i] = c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
}
const lin2srgb = (v) => {
  const c = v <= 0.0031308 ? v * 12.92 : 1.055 * v ** (1 / 2.4) - 0.055;
  return Math.max(0, Math.min(255, Math.round(c * 255)));
};

function resize(src, dw, dh) {
  const { width: sw, height: sh, data } = src;
  if (sw === dw && sh === dh) return { width: dw, height: dh, data: Buffer.from(data) };

  const acc = new Float64Array(dw * dh * 4);
  const sx = sw / dw, sy = sh / dh;

  for (let dy = 0; dy < dh; dy++) {
    const y0 = dy * sy, y1 = (dy + 1) * sy;
    const iy0 = Math.floor(y0), iy1 = Math.min(sh, Math.ceil(y1));
    for (let dx = 0; dx < dw; dx++) {
      const x0 = dx * sx, x1 = (dx + 1) * sx;
      const ix0 = Math.floor(x0), ix1 = Math.min(sw, Math.ceil(x1));
      let r = 0, g = 0, b = 0, a = 0, wsum = 0;
      for (let y = iy0; y < iy1; y++) {
        const wy = Math.min(y + 1, y1) - Math.max(y, y0);
        if (wy <= 0) continue;
        for (let x = ix0; x < ix1; x++) {
          const wx = Math.min(x + 1, x1) - Math.max(x, x0);
          if (wx <= 0) continue;
          const wgt = wx * wy;
          const i = (y * sw + x) * 4;
          const al = data[i + 3] / 255;
          r += SRGB2LIN[data[i]] * al * wgt;
          g += SRGB2LIN[data[i + 1]] * al * wgt;
          b += SRGB2LIN[data[i + 2]] * al * wgt;
          a += al * wgt;
          wsum += wgt;
        }
      }
      const o = (dy * dw + dx) * 4;
      if (wsum > 0) { acc[o] = r / wsum; acc[o + 1] = g / wsum; acc[o + 2] = b / wsum; acc[o + 3] = a / wsum; }
    }
  }

  const px = Buffer.alloc(dw * dh * 4);
  for (let i = 0; i < dw * dh; i++) {
    const o = i * 4, a = acc[o + 3];
    if (a > 1e-6) { // 언프리멀티플라이 — 투명 픽셀은 RGB 가 의미 없으므로 0 으로 둔다
      px[o] = lin2srgb(acc[o] / a);
      px[o + 1] = lin2srgb(acc[o + 1] / a);
      px[o + 2] = lin2srgb(acc[o + 2] / a);
    }
    px[o + 3] = Math.max(0, Math.min(255, Math.round(a * 255)));
  }
  return { width: dw, height: dh, data: px };
}

// ─────────────────────────────────────────────────────────────────────────────
// PNG 인코드 / ICO 컨테이너
// ─────────────────────────────────────────────────────────────────────────────
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

function encodePng(img) {
  const { width: w, height: h, data } = img;
  const raw = Buffer.alloc(h * (w * 4 + 1));
  for (let y = 0; y < h; y++) {
    raw[y * (w * 4 + 1)] = 0; // 필터 없음
    data.copy(raw, y * (w * 4 + 1) + 1, y * w * 4, (y + 1) * w * 4);
  }
  const chunk = (type, d) => {
    const len = Buffer.alloc(4); len.writeUInt32BE(d.length, 0);
    const t = Buffer.from(type, 'ascii');
    const crc = Buffer.alloc(4); crc.writeUInt32BE(crc32(Buffer.concat([t, d])), 0);
    return Buffer.concat([len, t, d, crc]);
  };
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(w, 0); ihdr.writeUInt32BE(h, 4);
  ihdr[8] = 8; // bit depth
  ihdr[9] = 6; // color type RGBA
  return Buffer.concat([
    PNG_SIG, chunk('IHDR', ihdr),
    chunk('IDAT', zlib.deflateSync(raw, { level: 9 })),
    chunk('IEND', Buffer.alloc(0)),
  ]);
}

/** 투명 여백을 잘라내고 정사각으로 맞춘다.
 *
 *  앱 아이콘 마스터는 macOS Dock 규칙상 캔버스의 80% 만 아트고 나머지는 투명 여백이다.
 *  그 여백째로 줄이면 브라우저 툴바(확장 아이콘)나 화면 안 로고에서 혼자 작아 보인다 —
 *  Dock 여백은 macOS 만의 관례라 그 밖에서는 프레임을 꽉 채우는 게 맞다. 잘라낸 뒤엔
 *  원래 비율이 깨지지 않도록 긴 변 기준 정사각으로 다시 채운다(가운데 정렬).
 */
function trimToSquare(img, threshold = 8) {
  const { width: w, height: h, data } = img;
  let x0 = w, y0 = h, x1 = -1, y1 = -1;
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      if (data[(y * w + x) * 4 + 3] > threshold) {
        if (x < x0) x0 = x;
        if (x > x1) x1 = x;
        if (y < y0) y0 = y;
        if (y > y1) y1 = y;
      }
    }
  }
  if (x1 < 0) return img; // 전부 투명 — 손대지 않는다
  const cw = x1 - x0 + 1, chh = y1 - y0 + 1;
  const side = Math.max(cw, chh);
  const ox = Math.floor((side - cw) / 2), oy = Math.floor((side - chh) / 2);
  const out = Buffer.alloc(side * side * 4);
  for (let y = 0; y < chh; y++) {
    const src = ((y0 + y) * w + x0) * 4;
    data.copy(out, ((oy + y) * side + ox) * 4, src, src + cw * 4);
  }
  return { width: side, height: side, data: out };
}

/** Vista+ ICO — 각 엔트리를 PNG 로 담는다(비트맵보다 작고 256px 를 지원). */
function encodeIco(images) {
  const pngs = images.map(encodePng);
  const header = Buffer.alloc(6);
  header.writeUInt16LE(0, 0);
  header.writeUInt16LE(1, 2); // type: icon
  header.writeUInt16LE(images.length, 4);

  const dir = Buffer.alloc(16 * images.length);
  let offset = 6 + dir.length;
  images.forEach((img, i) => {
    const e = i * 16;
    dir[e] = img.width >= 256 ? 0 : img.width;    // 256 은 0 으로 표기하는 규격
    dir[e + 1] = img.height >= 256 ? 0 : img.height;
    dir.writeUInt16LE(1, e + 4);                  // color planes
    dir.writeUInt16LE(32, e + 6);                 // bits per pixel
    dir.writeUInt32LE(pngs[i].length, e + 8);
    dir.writeUInt32LE(offset, e + 12);
    offset += pngs[i].length;
  });
  return Buffer.concat([header, dir, ...pngs]);
}

// ─────────────────────────────────────────────────────────────────────────────
// 입력 로드 + 검증
// ─────────────────────────────────────────────────────────────────────────────
const warnings = [];

function loadImage(file, label, minSize) {
  const img = decodePng(fs.readFileSync(file));
  if (img.width !== img.height) throw new Error(`${label} 는 정사각이어야 합니다 (현재 ${img.width}x${img.height})`);
  if (img.width < minSize) throw new Error(`${label} 는 ${minSize}px 이상이어야 합니다 (현재 ${img.width}px)`);
  return img;
}

/** 단일 파일 또는 프레임 폴더를 읽어 항상 프레임 배열로 돌려준다. */
function loadFrames(name, minSize) {
  const dir = path.join(SRC, name);
  if (fs.existsSync(dir) && fs.statSync(dir).isDirectory()) {
    const files = fs.readdirSync(dir).filter(f => f.toLowerCase().endsWith('.png')).sort();
    if (!files.length) throw new Error(`${name}/ 폴더에 PNG 프레임이 없습니다`);
    return { frames: files.map(f => loadImage(path.join(dir, f), `${name}/${f}`, minSize)), animated: true };
  }
  const file = path.join(SRC, `${name}.png`);
  if (!fs.existsSync(file)) return null;
  return { frames: [loadImage(file, `${name}.png`, minSize)], animated: false };
}

/** macOS 템플릿 이미지 전제(알파만 유효)를 실제로 지키는지 본다. 과거에 배경이 불투명한
 *  PNG 를 넣어 트레이가 통짜 사각형으로 칠해진 적이 있다(main/tray.js 주석 참고). */
function checkMacTemplate(img, label) {
  const d = img.data;
  let opaque = 0, colored = 0;
  for (let i = 0; i < d.length; i += 4) {
    if (d[i + 3] <= 8) continue;
    opaque++;
    if (Math.max(d[i], d[i + 1], d[i + 2]) - Math.min(d[i], d[i + 1], d[i + 2]) > 24) colored++;
  }
  const total = d.length / 4;
  if (opaque / total > 0.97) {
    warnings.push(`${label}: 거의 전부 불투명합니다. macOS 템플릿 이미지는 알파를 마스크로 쓰므로, `
      + '배경이 투명하지 않으면 트레이가 통짜 사각형으로 칠해집니다.');
  }
  if (colored / Math.max(1, opaque) > 0.1) {
    warnings.push(`${label}: 채도가 있는 픽셀이 많습니다. macOS 템플릿 이미지는 RGB 를 버리고 알파만 `
      + '읽으므로 색이 화면에 반영되지 않습니다 — 단색 검정 실루엣으로 그리는 것이 맞습니다.');
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// 실행
// ─────────────────────────────────────────────────────────────────────────────
const written = [];
function write(abs, buf) {
  fs.mkdirSync(path.dirname(abs), { recursive: true });
  fs.writeFileSync(abs, buf);
  written.push(`${path.relative(REPO, abs).replace(/\\/g, '/')}  (${(buf.length / 1024).toFixed(1)}KB)`);
}
const writePng = (abs, img, size) => write(abs, encodePng(resize(img, size, size)));

function main() {
  if (!fs.existsSync(SRC)) {
    throw new Error(`마스터 폴더가 없습니다: ${path.relative(REPO, SRC).replace(/\\/g, '/')}\n`
      + '  app-icon.png / tray-mac.png / tray-win.png 를 넣고 다시 실행하세요.');
  }

  // ── 앱 아이콘 (정지 이미지 — Dock/작업표시줄은 애니메이션 불가) ──
  const appFile = path.join(SRC, 'app-icon.png');
  if (!fs.existsSync(appFile)) throw new Error('assets/source/app-icon.png 가 필요합니다 (1024x1024 이상)');
  const app = loadImage(appFile, 'app-icon.png', 1024);
  if (fs.existsSync(path.join(SRC, 'app-icon'))) {
    warnings.push('app-icon/ 폴더는 무시했습니다 — 앱 아이콘은 OS 가 애니메이션을 지원하지 않습니다.');
  }

  writePng(path.join(DESKTOP, 'build', 'icon.png'), app, 1024);  // electron-builder 원본(dmg/exe)
  writePng(path.join(DESKTOP, 'assets', 'icon.png'), app, 256);  // BrowserWindow (mac/linux)
  write(path.join(DESKTOP, 'assets', 'icon.ico'),                // BrowserWindow (Windows, 멀티 해상도)
    encodeIco([16, 24, 32, 48, 64, 128, 256].map(s => resize(app, s, s))));

  // 확장 아이콘과 UI 브랜드 이미지는 여백 없는 mark 에서 뽑는다 — 앱 마스터의 80%
  // Dock 여백을 그대로 줄이면 브라우저 툴바/화면 안에서 혼자 작아 보인다.
  const markFile = path.join(SRC, 'app-icon-mark.png');
  const mark = fs.existsSync(markFile) ? loadImage(markFile, 'app-icon-mark.png', 1024) : app;
  if (mark === app) {
    warnings.push('app-icon-mark.png 이 없어 확장/UI 이미지도 앱 마스터에서 뽑았습니다 — '
      + 'Dock 여백이 함께 줄어들어 작아 보일 수 있습니다.');
  }
  const markTrimmed = trimToSquare(mark);
  for (const s of [16, 32, 48, 128]) {
    writePng(path.join(REPO, 'extension', 'icons', `icon${s}.png`), markTrimmed, s);
  }
  // 화면에 보이는 브랜드 일러스트 — 표시 크기의 2배로 만든다(CSS 가 줄여서 그린다).
  // 앱 홈 화면과 확장 사이드바에는 아이콘 마크가 아니라 별도 일러스트(ui-hero.png)를 쓴다:
  // 이 두 자리는 100px 안팎으로 크게 보여주는 곳이라, 16px 트레이까지 견디려고 단순화한
  // 마크를 확대하면 휑해 보인다. ui-hero.png 가 없으면 예전처럼 마크에서 뽑는다.
  const heroFile = path.join(SRC, 'ui-hero.png');
  const hero = fs.existsSync(heroFile) ? loadImage(heroFile, 'ui-hero.png', 224) : markTrimmed;
  if (hero === markTrimmed) {
    warnings.push('ui-hero.png 이 없어 홈/사이드바 일러스트도 아이콘 마크에서 뽑았습니다.');
  }
  writePng(path.join(DESKTOP, 'assets', 'figma', 'home-hero.png'), hero, 224);        // 표시 104x107
  writePng(path.join(REPO, 'extension', 'sidepanel', 'assets', 'agent-shield.png'), hero, 144); // 표시 72

  // ── 트레이 (애니메이션 가능 — 프레임 폴더면 프레임별로 생성) ──
  const SPECS = [
    { name: 'tray-mac', minSize: 44, sizes: [22, 44], base: 'tray-icon', tag: 'mac' },
    { name: 'tray-win', minSize: 32, sizes: [16, 32], base: 'tray-icon-win', tag: 'win' },
  ];
  const manifest = { frameIntervalMs: FRAME_INTERVAL_MS };

  for (const spec of SPECS) {
    const loaded = loadFrames(spec.name, spec.minSize);
    if (!loaded) throw new Error(`assets/source/${spec.name}.png (또는 ${spec.name}/ 폴더)가 필요합니다`);
    const { frames, animated } = loaded;
    if (spec.tag === 'mac') frames.forEach((f, i) => checkMacTemplate(f, `tray-mac 프레임 ${i + 1}`));

    // 정지 상태 대표 = 1번 프레임. @2x 는 Electron nativeImage 가 자동 인식한다.
    const [s1, s2] = spec.sizes;
    writePng(path.join(DESKTOP, 'assets', `${spec.base}.png`), frames[0], s1);
    writePng(path.join(DESKTOP, 'assets', `${spec.base}@2x.png`), frames[0], s2);

    if (animated) {
      manifest[spec.tag] = frames.map((f, i) => {
        const n = `${spec.tag}-${String(i + 1).padStart(2, '0')}`;
        writePng(path.join(DESKTOP, 'assets', 'tray-frames', `${n}.png`), f, s1);
        writePng(path.join(DESKTOP, 'assets', 'tray-frames', `${n}@2x.png`), f, s2);
        return `${n}.png`;
      });
    }
  }

  if (manifest.mac || manifest.win) {
    write(path.join(DESKTOP, 'assets', 'tray-frames', 'manifest.json'),
      Buffer.from(JSON.stringify(manifest, null, 2) + '\n', 'utf-8'));
  }

  console.log('생성 완료:');
  written.forEach(f => console.log('  ' + f));

  if (warnings.length) {
    console.log('\n경고:');
    warnings.forEach(w => console.log('  ! ' + w));
  }

  const mf = path.join(REPO, 'extension', 'manifest.json');
  if (fs.existsSync(mf) && !fs.readFileSync(mf, 'utf-8').includes('icon32.png')) {
    console.log('\n다음 단계: extension/manifest.json 의 "icons" 와 "action.default_icon" 에 '
      + '"32": "icons/icon32.png" 를 추가하세요 (파일은 방금 생성됨).');
  }
  if (manifest.mac || manifest.win) {
    console.log('다음 단계: 트레이 프레임이 생성됐습니다. main/tray.js 는 아직 정지 아이콘만 쓰므로, '
      + 'tray-frames/manifest.json 을 읽어 setImage 를 돌리는 재생 코드가 필요합니다.');
  }
}

try {
  main();
} catch (err) {
  console.error(`아이콘 생성 실패: ${err.message}`);
  process.exit(1);
}
