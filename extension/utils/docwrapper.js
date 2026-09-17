/**
 * docwrapper.js
 * 마스킹된 텍스트를 AI 사이트에 업로드할 파일로 바꾼다.
 *
 * wrapMaskedFile(text, mimeType, origFileName)
 *   → { bytes: Uint8Array, mimeType: string, fileName: string }
 *
 * 항상 .md(text/plain) 로 낸다:
 *   - 파서가 이미 레이아웃을 버렸으므로 원본 포맷을 재구성할 이유가 없다.
 *   - MD 는 ChatGPT/Claude/Gemini/Copilot 이 전부 받는다.
 *   - DOCX/PDF 재구성보다 파일이 작아 업로드도 빠르다.
 *   - 마스킹 토큰([이름 마스킹] 등)도 더 명확히 인식된다.
 *
 * 예전엔 wrapAsDocx(ZIP+OOXML 직접 조립)와 wrapAsPdf(UTF-16BE hex 스트림 +
 * Type0/CIDFont + ToUnicode CMap)를 "직접 다운로드용" 이라며 들고 있었다. 그런데
 * wrapMaskedFile 은 처음부터 무조건 MD 로만 갔고 둘을 부르는 곳이 어디에도 없었다.
 * 딸린 CRC32 테이블·ZIP 빌더·XML 이스케이프·hex 변환도 그 둘 전용이라 같이 지웠다.
 */

const _enc = new TextEncoder();

export function wrapAsMd(maskedText, origFileName) {
  const bytes = _enc.encode(maskedText);
  const fileName = origFileName
    .replace(/(\.[^.]+)$/, '_masked.md')
    .replace(/^([^.]+)$/, '$1_masked.md');
  return { bytes, mimeType: 'text/plain', fileName };
}

export function wrapMaskedFile(maskedText, mimeType, origFileName) {
  return wrapAsMd(maskedText, origFileName);
}
