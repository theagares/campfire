/**
 * mask-segments.js
 * 탐지 결과(원문 + piiItems/injectionItems)를 "텍스트 구간 + 마스킹 항목" 으로 쪼개고,
 * 사용자의 해제 선택을 반영해 최종 문자열을 만든다.
 *
 * 왜 sidepanel.js 밖으로 꺼냈나 — 패널이 보여준 미리보기와 실제로 업로드되는 파일이
 * **다른 코드로** 만들어지면 언제든 갈라진다. 다중 첨부에서는 최종본을 만드는 주체가
 * 패널이 아니라 SW 라서(패널은 바이너리를 만들지 않는다) 더더욱 같은 함수를 써야 한다.
 * 그래서 이 파일에는 DOM 도 state 도 두지 않는다 — 입력만 받아 값을 돌려주는 순수 함수다.
 *
 * 항목 식별자가 숫자가 아니라 문자열 키인 이유:
 *   다중 첨부는 파일이 비동기로 끝나고 중간에 실패하는 항목도 생긴다. 예전처럼 배열
 *   전체에 누적 offset 으로 번호를 매기면, 항목 하나가 늘거나 빠질 때마다 뒤 번호가
 *   전부 밀려 **사용자가 이미 바꿔 둔 마스킹 선택이 다른 항목을 가리킨다**.
 *   `계약서-3:0` 처럼 소유자 접두사를 붙이면 그 항목이 사라지지 않는 한 키가 변하지 않는다.
 */

export const TYPE_LABELS = {
  PERSON_NAME: '이름', EMAIL: '이메일', PHONE: '전화번호', ADDRESS: '주소',
  ID_NUMBER: '신분증번호', CREDIT_CARD: '카드번호', DATE_OF_BIRTH: '생년월일',
  ORGANIZATION: '조직기밀', BANK_ACCOUNT: '계좌번호', OTHER_PII: '개인정보',
  CREDENTIAL: '자격증명',
  USER_DEFINED_TERM: '사용자 지정',
  INSTRUCTION_OVERRIDE: '명령 재정의', ROLE_MANIPULATION: '역할 조작',
  SYSTEM_PROMPT_LEAK: '시스템 프롬프트 유출', JAILBREAK: '탈옥 시도',
  HIDDEN_COMMAND: '숨겨진 명령', DATA_EXFILTRATION: '데이터 유출 시도',
  OTHER_INJECTION: '프롬프트 인젝션',
};

export const labelOf = (t) => TYPE_LABELS[t] ?? t;

/** 마스킹 자리표시자. 엔진 masker.py 의 `[{label} 마스킹]` 과 같은 형태여야 한다. */
export const maskTokenFor = (label) => `[${label} 마스킹]`;

/**
 * 원문을 text/item 세그먼트 배열로 쪼갠다.
 * keyPrefix 는 항목 키의 소유자(문서 id 또는 'prompt').
 */
export function buildSegments(text, piiItems, injectionItems, keyPrefix = 'doc', forcedMaskItems = []) {
  const src = typeof text === 'string' ? text : '';
  // 엔진(파이썬)의 start/end 는 코드포인트 기준이고 JS 문자열은 UTF-16 이다. 이모지처럼
  // BMP 밖 글자가 앞에 있으면 그 수만큼 밀려 엉뚱한 곳을 가리고 원문이 그대로 남았다
  // ("🔥🔥🔥 Project Aurora" → "🔥🔥[마스킹]ora"). 여기 한 곳에서 바꾼다 — 패널 미리보기와
  // SW 최종본이 모두 이 함수를 지난다.
  let unit = null;
  if (/[\uD800-\uDBFF]/.test(src)) {
    unit = [0];
    for (const ch of src) unit.push(unit[unit.length - 1] + ch.length);
  }
  const at = (cp) => (unit && Number.isInteger(cp) ? unit[Math.min(Math.max(cp, 0), unit.length - 1)] : cp);
  const detected = [
    ...(piiItems || []).map(i => ({ ...i, cat: 'pii' })),
    ...(injectionItems || []).map(i => ({ ...i, cat: 'inj' })),
    ...(forcedMaskItems || []).map(i => ({ ...i, cat: 'forced', mandatory: true })),
  ].map(i => ({ ...i, start: at(i.start), end: at(i.end) }))
    .filter(i => Number.isInteger(i.start) && Number.isInteger(i.end) && i.start >= 0 && i.end > i.start)
    .sort((a, b) => a.start - b.start || Number(!!b.mandatory) - Number(!!a.mandatory) || b.end - a.end);

  // 겹친 탐지는 하나의 구간으로 합친다. 그중 하나라도 사용자 지정 규칙이면 합쳐진
  // 전체 구간이 필수 마스킹이다. 일반 탐지가 바깥을 감싼다는 이유로 내부의 필수
  // 규칙이 사라지면 해제 토글로 원문을 보낼 수 있기 때문이다.
  const all = [];
  for (const item of detected) {
    const prev = all[all.length - 1];
    if (!prev || item.start >= prev.end) {
      all.push({ ...item });
      continue;
    }
    prev.end = Math.max(prev.end, item.end);
    if (item.mandatory) {
      prev.mandatory = true;
      prev.cat = 'forced';
      prev.type = 'USER_DEFINED_TERM';
    }
  }

  const segs = [];
  let cursor = 0, n = 0;
  for (const it of all) {
    // 겹치거나 역순인 탐지는 앞선 구간을 이긴다 — cursor 를 되돌리지 않아야
    // 같은 글자가 두 번 나오거나 사라지지 않는다.
    if (it.end <= cursor) continue;
    const start = Math.max(it.start, cursor);
    if (start > cursor) segs.push({ type: 'text', text: src.slice(cursor, start) });
    const original = src.slice(start, it.end);
    if (original) {
      segs.push({
        type: 'item',
        key: `${keyPrefix}:${n++}`,
        cat: it.cat,
        dtype: it.type,
        label: labelOf(it.type),
        original,
        locked: !!it.mandatory,
      });
    }
    cursor = it.end;
  }
  if (cursor < src.length) segs.push({ type: 'text', text: src.slice(cursor) });
  return segs;
}

/**
 * 세그먼트 + 해제 키 집합 → 실제로 전송될 문자열.
 * unmaskedKeys 에 든 항목만 원문을 유지하고 나머지는 자리표시자로 바뀐다.
 * 패널 미리보기와 SW 산출물이 반드시 이 함수 하나를 공유한다.
 */
export function buildFinalText(segments, unmaskedKeys) {
  const keep = unmaskedKeys instanceof Set ? unmaskedKeys : new Set(unmaskedKeys || []);
  return (segments || []).map(seg => {
    if (seg.type === 'text') return seg.text;
    return !seg.locked && keep.has(seg.key) ? seg.original : maskTokenFor(seg.label);
  }).join('');
}

/** 원문 + 탐지 결과 + 해제 키 → 최종 문자열. SW 가 결정 시점에 쓰는 한 줄 경로. */
export function finalTextFrom(text, piiItems, injectionItems, keyPrefix, unmaskedKeys, forcedMaskItems = []) {
  return buildFinalText(buildSegments(text, piiItems, injectionItems, keyPrefix, forcedMaskItems), unmaskedKeys);
}
