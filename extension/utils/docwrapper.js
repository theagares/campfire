/**
 * docwrapper.js
 * 마스킹된 텍스트를 원본 포맷(DOCX/PDF)으로 래핑하는 순수 JS 유틸리티.
 *
 * wrapMaskedFile(text, mimeType, origFileName)
 *   → { bytes: Uint8Array, mimeType: string, fileName: string }
 */

const _enc = new TextEncoder();

// ══════════════════════════════════════════════════════════════════
// CRC32 (ZIP용)
// ══════════════════════════════════════════════════════════════════
const _CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let i = 0; i < 256; i++) {
    let c = i;
    for (let j = 0; j < 8; j++) c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
    t[i] = c;
  }
  return t;
})();

function crc32(buf) {
  let c = 0xFFFFFFFF;
  for (const b of buf) c = _CRC_TABLE[(c ^ b) & 0xFF] ^ (c >>> 8);
  return (c ^ 0xFFFFFFFF) >>> 0;
}

// ══════════════════════════════════════════════════════════════════
// 최소 ZIP 빌더 (STORE, 무압축)
// ══════════════════════════════════════════════════════════════════
function buildZip(entries, targetSize = 0) {
  const now = new Date();
  const dd = ((now.getFullYear() - 1980) << 9) | ((now.getMonth() + 1) << 5) | now.getDate();
  const dt = (now.getHours() << 11) | (now.getMinutes() << 5) | (now.getSeconds() >> 1);

  function u16(v) { return new Uint8Array([v & 0xFF, (v >> 8) & 0xFF]); }
  function u32(v) {
    v >>>= 0;
    return new Uint8Array([v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF, (v >> 24) & 0xFF]);
  }
  function concat(...arrs) {
    const total = arrs.reduce((s, a) => s + a.length, 0);
    const out = new Uint8Array(total);
    let p = 0;
    for (const a of arrs) { out.set(a, p); p += a.length; }
    return out;
  }

  const localParts  = [];
  const centralParts = [];
  let offset = 0;

  for (const { name, data } of entries) {
    const nb  = _enc.encode(name);
    const crc = crc32(data);
    const sz  = data.length;

    // Local file header (30 bytes + name)
    const lh = concat(
      new Uint8Array([0x50, 0x4B, 0x03, 0x04, 20, 0, 0, 0, 0, 0]),
      u16(dt), u16(dd), u32(crc), u32(sz), u32(sz),
      u16(nb.length), u16(0),
      nb,
    );

    // Central directory entry (46 bytes + name)
    const ch = concat(
      new Uint8Array([0x50, 0x4B, 0x01, 0x02, 20, 0, 20, 0, 0, 0, 0, 0]),
      u16(dt), u16(dd), u32(crc), u32(sz), u32(sz),
      u16(nb.length), u16(0), u16(0), u16(0), u16(0), u32(0),
      u32(offset),
      nb,
    );

    localParts.push(lh, data);
    centralParts.push(ch);
    offset += lh.length + sz;
  }

  const cdSize  = centralParts.reduce((s, p) => s + p.length, 0);
  const cdStart = offset;

  // targetSize가 주어지면 EOCD comment로 파일을 해당 크기로 패딩
  // ZIP EOCD comment 최대 65535 bytes
  const eocdBase = 22; // EOCD 고정 헤더 크기 (comment 길이 필드 포함)
  const baseSize = localParts.reduce((s,p)=>s+p.length,0)
                 + centralParts.reduce((s,p)=>s+p.length,0)
                 + eocdBase;
  const commentLen = (targetSize && targetSize > baseSize && (targetSize - baseSize) <= 65535)
    ? targetSize - baseSize : 0;

  const eocd = concat(
    new Uint8Array([0x50, 0x4B, 0x05, 0x06, 0, 0, 0, 0]),
    u16(entries.length), u16(entries.length),
    u32(cdSize), u32(cdStart), u16(commentLen),
    new Uint8Array(commentLen), // padding (null bytes)
  );

  const all = [...localParts, ...centralParts, eocd];
  const total = all.reduce((s, p) => s + p.length, 0);
  const buf = new Uint8Array(total);
  let p = 0;
  for (const a of all) { buf.set(a, p); p += a.length; }
  return buf;
}

// ══════════════════════════════════════════════════════════════════
// DOCX 생성 (OOXML / UTF-8 → 한국어 완벽 지원)
// ══════════════════════════════════════════════════════════════════
function xmlEsc(s) {
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&apos;');
}

export function wrapAsDocx(maskedText, origFileName, targetSize = 0) {
  const paras = maskedText.split('\n').map(line =>
    line.trim()
      ? `<w:p><w:r><w:rPr>` +
        `<w:rFonts w:ascii="Malgun Gothic" w:eastAsia="Malgun Gothic" w:hAnsi="Malgun Gothic"/>` +
        `</w:rPr><w:t xml:space="preserve">${xmlEsc(line)}</w:t></w:r></w:p>`
      : '<w:p/>'
  ).join('');

  const now = new Date().toISOString();
  const entries = [
    {
      name: '[Content_Types].xml',
      data: _enc.encode(
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">' +
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>' +
        '<Default Extension="xml" ContentType="application/xml"/>' +
        '<Override PartName="/word/document.xml"' +
        ' ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>' +
        '<Override PartName="/word/settings.xml"' +
        ' ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>' +
        '<Override PartName="/word/styles.xml"' +
        ' ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>' +
        '<Override PartName="/docProps/core.xml"' +
        ' ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>' +
        '<Override PartName="/docProps/app.xml"' +
        ' ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>' +
        '</Types>'
      ),
    },
    {
      name: '_rels/.rels',
      data: _enc.encode(
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
        '<Relationship Id="rId1"' +
        ' Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"' +
        ' Target="word/document.xml"/>' +
        '<Relationship Id="rId2"' +
        ' Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties"' +
        ' Target="docProps/core.xml"/>' +
        '<Relationship Id="rId3"' +
        ' Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties"' +
        ' Target="docProps/app.xml"/>' +
        '</Relationships>'
      ),
    },
    {
      name: 'word/document.xml',
      data: _enc.encode(
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"' +
        ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">' +
        '<w:body>' + paras + '<w:sectPr/></w:body></w:document>'
      ),
    },
    {
      name: 'word/_rels/document.xml.rels',
      data: _enc.encode(
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'
      ),
    },
    {
      name: 'word/settings.xml',
      data: _enc.encode(
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
        '<w:settings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">' +
        '<w:defaultTabStop w:val="720"/>' +
        '</w:settings>'
      ),
    },
    {
      name: 'word/styles.xml',
      data: _enc.encode(
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
        '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">' +
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normal">' +
        '<w:name w:val="Normal"/></w:style>' +
        '</w:styles>'
      ),
    },
    {
      name: 'docProps/core.xml',
      data: _enc.encode(
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"' +
        ' xmlns:dc="http://purl.org/dc/elements/1.1/"' +
        ' xmlns:dcterms="http://purl.org/dc/terms/"' +
        ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">' +
        '<dc:creator>SecureDoc</dc:creator>' +
        '<dcterms:created xsi:type="dcterms:W3CDTF">' + now + '</dcterms:created>' +
        '</cp:coreProperties>'
      ),
    },
    {
      name: 'docProps/app.xml',
      data: _enc.encode(
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">' +
        '<Application>Microsoft Office Word</Application>' +
        '<DocSecurity>0</DocSecurity>' +
        '<ScaleCrop>false</ScaleCrop>' +
        '<SharedDoc>false</SharedDoc>' +
        '</Properties>'
      ),
    },
  ];

  const DOCX_MIME = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document';
  const fileName  = origFileName.replace(/(\.[^.]+)$/, '_masked$1') || 'masked_document.docx';
  return { bytes: buildZip(entries, targetSize), mimeType: DOCX_MIME, fileName };
}

// ══════════════════════════════════════════════════════════════════
// PDF 생성 (UTF-16BE hex 스트림 + Type0/CIDFont + ToUnicode CMap)
// 한국어 텍스트 포함 가능, EC2 서버의 문서 파서가 처리할 수 있는 형태
// ══════════════════════════════════════════════════════════════════

/** JavaScript 문자열 → UTF-16BE hex 문자열 (PDF hex string용) */
function toHex16(str) {
  let hex = 'FEFF'; // BOM
  for (let i = 0; i < str.length; i++) {
    hex += str.charCodeAt(i).toString(16).padStart(4, '0');
  }
  return hex;
}

export function wrapAsPdf(maskedText, origFileName) {
  const W = 595, H = 842; // A4
  const marginX = 50, marginY = 50;
  const fontSize = 10, lineH = 14;
  const charsPerLine = 85;
  const linesPerPage = Math.floor((H - 2 * marginY) / lineH);

  // 줄 바꿈 처리
  const allLines = [];
  for (const raw of maskedText.split('\n')) {
    if (!raw.length) { allLines.push(''); continue; }
    let rem = raw;
    while (rem.length > charsPerLine) {
      let bp = rem.lastIndexOf(' ', charsPerLine);
      if (bp < 1) bp = charsPerLine;
      allLines.push(rem.slice(0, bp));
      rem = rem.slice(bp).trimStart();
    }
    allLines.push(rem);
  }
  if (!allLines.length) allLines.push('');

  // 페이지 분할
  const pages = [];
  for (let i = 0; i < allLines.length; i += linesPerPage)
    pages.push(allLines.slice(i, i + linesPerPage));

  // ── PDF 빌드 ────────────────────────────────────────────────────
  const parts  = [];          // Uint8Array 청크 목록
  const bOff   = [0];         // 누적 바이트 수 (공유 레퍼런스)
  const objOff = {};          // 객체 ID → 오프셋

  function emit(s) {
    const b = _enc.encode(s);
    parts.push(b);
    bOff[0] += b.length;
  }
  function emitRaw(bytes) {
    parts.push(bytes);
    bOff[0] += bytes.length;
  }

  function dictObj(id, dict) {
    objOff[id] = bOff[0];
    emit(`${id} 0 obj\n<< ${dict} >>\nendobj\n`);
  }
  function streamObj(id, dict, streamStr) {
    objOff[id] = bOff[0];
    const sb = _enc.encode(streamStr);
    emit(`${id} 0 obj\n<< ${dict} /Length ${sb.length} >>\nstream\n`);
    emitRaw(sb);
    emit('\nendstream\nendobj\n');
  }

  // 헤더
  emit('%PDF-1.4\n');

  const nP         = pages.length;
  // 객체 ID 할당
  const ID_CATALOG  = 1;
  const ID_PAGES    = 2;
  const ID_PAGES_S  = 3;                      // 페이지 객체 시작
  const ID_CONT_S   = 3 + nP;                 // 콘텐츠 스트림 시작
  const ID_FONT     = 3 + 2 * nP;
  const ID_CMAP     = ID_FONT + 1;
  const ID_DESC     = ID_FONT + 2;
  const TOTAL_OBJS  = ID_FONT + 3;

  const pageIds    = Array.from({ length: nP }, (_, i) => ID_PAGES_S + i);
  const contentIds = Array.from({ length: nP }, (_, i) => ID_CONT_S + i);

  // 카탈로그 / 페이지 트리
  dictObj(ID_CATALOG, `/Type /Catalog /Pages ${ID_PAGES} 0 R`);
  dictObj(ID_PAGES,
    `/Type /Pages /Count ${nP} /Kids [${pageIds.map(id => `${id} 0 R`).join(' ')}]`
  );

  // 페이지 객체
  for (let i = 0; i < nP; i++) {
    dictObj(pageIds[i],
      `/Type /Page /Parent ${ID_PAGES} 0 R ` +
      `/MediaBox [0 0 ${W} ${H}] ` +
      `/Contents ${contentIds[i]} 0 R ` +
      `/Resources << /Font << /F1 ${ID_FONT} 0 R >> >>`
    );
  }

  // 콘텐츠 스트림 (각 페이지)
  for (let i = 0; i < nP; i++) {
    const lines = pages[i];
    let s = `BT\n/F1 ${fontSize} Tf\n`;
    s += `1 0 0 1 ${marginX} ${H - marginY} Tm\n`;
    s += `${lineH} TL\n`;
    for (const line of lines) {
      s += `<${toHex16(line || ' ')}> Tj T*\n`;
    }
    s += 'ET\n';
    streamObj(contentIds[i], ``, s);
  }

  // ToUnicode CMap (Identity: 코드포인트 = 유니코드)
  const cmap =
    '/CIDInit /ProcSet findresource begin\n' +
    '12 dict begin\nbegincmap\n' +
    '/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n' +
    '/CMapName /Adobe-Identity-UCS def\n' +
    '/CMapType 2 def\n' +
    '1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n' +
    '1 beginbfrange\n<0000> <FFFF> <0000>\nendbfrange\n' +
    'endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n';
  streamObj(ID_CMAP, '/Type /CMap', cmap);

  // CIDFont 디스센던트
  dictObj(ID_DESC,
    `/Type /Font /Subtype /CIDFontType2 /BaseFont /Arial ` +
    `/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> /DW 1000`
  );

  // Type0 폰트 (한국어 포함 유니코드 대응)
  dictObj(ID_FONT,
    `/Type /Font /Subtype /Type0 /BaseFont /Arial ` +
    `/Encoding /Identity-H ` +
    `/DescendantFonts [${ID_DESC} 0 R] ` +
    `/ToUnicode ${ID_CMAP} 0 R`
  );

  // xref 테이블
  const xrefOffset = bOff[0];
  let xref = `xref\n0 ${TOTAL_OBJS}\n`;
  xref += '0000000000 65535 f \n';
  for (let i = 1; i < TOTAL_OBJS; i++) {
    xref += `${String(objOff[i] ?? 0).padStart(10, '0')} 00000 n \n`;
  }
  emit(xref);
  emit(`trailer\n<< /Size ${TOTAL_OBJS} /Root ${ID_CATALOG} 0 R >>\nstartxref\n${xrefOffset}\n%%EOF\n`);

  // 청크 합치기
  const total = parts.reduce((s, p) => s + p.length, 0);
  const buf   = new Uint8Array(total);
  let pos = 0;
  for (const p of parts) { buf.set(p, pos); pos += p.length; }

  const fileName = origFileName.replace(/(\.[^.]+)$/, '_masked$1') || 'masked_document.pdf';
  return { bytes: buf, mimeType: 'application/pdf', fileName };
}

// ══════════════════════════════════════════════════════════════════
// MD 생성 (순수 텍스트, AI 이해도 최적)
// ══════════════════════════════════════════════════════════════════

/**
 * wrapAsMd
 * 마스킹된 텍스트를 .md(text/plain) 파일로 래핑한다.
 *
 * 파서가 이미 레이아웃을 버리고 텍스트만 추출하므로,
 * 원본 포맷(DOCX/PDF)으로 재구성할 이유가 없다.
 * MD(plain text)로 전달하면 AI가 구조화된 텍스트를 그대로 읽어
 * 마스킹 토큰([PERSON_NAME] 등)도 더 명확히 인식한다.
 */
export function wrapAsMd(maskedText, origFileName) {
  const bytes    = _enc.encode(maskedText);
  const fileName = origFileName.replace(/(\.[^.]+)$/, '_masked.md').replace(/^([^.]+)$/, '$1_masked.md');
  return { bytes, mimeType: 'text/plain', fileName };
}

// ══════════════════════════════════════════════════════════════════
// 공개 API
// ══════════════════════════════════════════════════════════════════

/**
 * wrapMaskedFile
 * 마스킹된 텍스트를 AI 사이트에 업로드할 파일로 변환한다.
 *
 * 출력 형식: MD (text/plain)
 *   - 파서가 이미 레이아웃을 버렸으므로 원본 포맷 재구성 불필요.
 *   - MD는 모든 주요 AI 사이트(ChatGPT/Claude/Gemini/Copilot)가 지원.
 *   - DOCX/PDF 재구성 대비 파일 크기가 작아 업로드 속도도 빠름.
 *
 * wrapAsDocx / wrapAsPdf 는 직접 다운로드용으로 유지됨.
 */
export function wrapMaskedFile(maskedText, mimeType, origFileName, targetSize = 0) {
  return wrapAsMd(maskedText, origFileName);
}
