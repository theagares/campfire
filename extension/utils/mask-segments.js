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
  ORGANIZATION: '기관명', BANK_ACCOUNT: '계좌번호', OTHER_PII: '개인정보',
  CREDENTIAL: '자격증명', CONFIDENTIAL: '기밀',
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
    // 기밀(회사 KG)은 문장 하나가 구간 하나다 — 안쪽 PII 를 자식으로 품는다(cat 'conf')
    ...(piiItems || []).map(i => ({ ...i, cat: i.type === 'CONFIDENTIAL' ? 'conf' : 'pii' })),
    ...(injectionItems || []).map(i => ({ ...i, cat: 'inj' })),
    ...(forcedMaskItems || []).map(i => ({ ...i, cat: 'forced', mandatory: true })),
  ].map(i => ({ ...i, start: at(i.start), end: at(i.end) }))
    .filter(i => Number.isInteger(i.start) && Number.isInteger(i.end) && i.start >= 0 && i.end > i.start)
    // 바깥 구간이 먼저 — 같은 자리에서 시작하는 기밀 문장이 안쪽 필수 단어보다 앞서야 품을 수 있다
    .sort((a, b) => a.start - b.start || b.end - a.end || Number(!!b.mandatory) - Number(!!a.mandatory));

  let n = 0;
  const toSegments = (items, from, to) => {
    const segs = [];
    let cursor = from;
    for (const it of mergeSpans(items)) {
      // 겹치거나 역순인 탐지는 앞선 구간을 이긴다 — cursor 를 되돌리지 않아야
      // 같은 글자가 두 번 나오거나 사라지지 않는다.
      if (it.end <= cursor) continue;
      const start = Math.max(it.start, cursor);
      if (start > cursor) segs.push({ type: 'text', text: src.slice(cursor, start) });
      const original = src.slice(start, it.end);
      if (original) {
        const seg = {
          type: 'item',
          key: `${keyPrefix}:${n++}`,
          cat: it.cat,
          dtype: it.type,
          label: labelOf(it.type),
          original,
          locked: !!it.mandatory,
        };
        // 솔라 판정을 못 해 근거 문장을 가린 기밀(엔진 source 'encoder') — 화면은 점선 테두리로 구분한다
        if (it.cat === 'conf' && it.source === 'encoder') seg.fallback = true;
        if (it.children) seg.children = toSegments(it.children, start, it.end);
        segs.push(seg);
      }
      cursor = it.end;
    }
    if (cursor < to) segs.push({ type: 'text', text: src.slice(cursor, to) });
    return segs;
  };
  return toSegments(detected, 0, src.length);
}

/**
 * 겹친 탐지는 하나의 구간으로 합친다. 그중 하나라도 사용자 지정 규칙이면 합쳐진
 * 전체 구간이 필수 마스킹이다. 일반 탐지가 바깥을 감싼다는 이유로 내부의 필수
 * 규칙이 사라지면 해제 토글로 원문을 보낼 수 있기 때문이다.
 * 예외: 기밀 문장 안에 완전히 든 탐지는 합치지 않고 자식으로 둔다 — 기밀을 풀어도 안쪽 PII 는
 * 자기 토글대로, 필수 단어는 잠긴 채로 남는다.
 * 인젝션 구간도 겹친 PII·필수 단어를 자식으로 품는다. 솔라 없이 잡힌 인젝션은 128토큰 덩어리라 안쪽 항목과
 * 겹치기 쉬운데, 합치면 덩어리 전체가 그 항목의 색·이름(필수 단어면 잠김)으로 바뀌었다. 덩어리 경계가
 * PII 가운데를 자르면 부모를 늘려 통째로 품는다(엔진 masker.merge_overlapping 과 같은 규칙).
 */
function mergeSpans(items) {
  const all = [];
  const leaf = (cat) => cat === 'pii' || cat === 'forced';
  for (const item of items) {
    const prev = all[all.length - 1];
    if (!prev || item.start >= prev.end) {
      all.push({ ...item });
      continue;
    }
    if (prev.cat === 'conf' && item.end <= prev.end) {
      (prev.children ||= []).push(item);
      continue;
    }
    if (prev.cat === 'inj' && leaf(item.cat)) {
      prev.end = Math.max(prev.end, item.end);
      (prev.children ||= []).push(item);
      continue;
    }
    if (item.cat === 'inj' && leaf(prev.cat)) {
      all[all.length - 1] = { ...item, start: prev.start, end: Math.max(prev.end, item.end), children: [prev] };
      continue;
    }
    prev.end = Math.max(prev.end, item.end);
    if (item.mandatory) {
      prev.mandatory = true;
      prev.cat = 'forced';
      prev.type = 'USER_DEFINED_TERM';
    }
  }
  return all;
}

/** 세그먼트 트리의 모든 항목(기밀 문장 안의 항목 포함), 문서 순서대로. */
export function itemSegments(segments) {
  return (segments || []).flatMap(s => (s.type === 'item' ? [s, ...itemSegments(s.children)] : []));
}

/**
 * 세그먼트 + 해제 키 집합 → 실제로 전송될 문자열.
 * unmaskedKeys 에 든 항목만 원문을 유지하고 나머지는 자리표시자로 바뀐다.
 * 패널 미리보기와 SW 산출물이 반드시 이 함수 하나를 공유한다.
 */
export function buildFinalText(segments, unmaskedKeys) {
  const keep = unmaskedKeys instanceof Set ? unmaskedKeys : new Set(unmaskedKeys || []);
  const render = (segs) => (segs || []).map(seg => {
    if (seg.type === 'text') return seg.text;
    // 인젝션은 유형과 관계없이 엔진과 같은 [인젝션 마스킹](masker.py INJECTION_LABEL). 목록 이름은 유형별 그대로.
    if (seg.locked || !keep.has(seg.key)) return maskTokenFor(seg.cat === 'inj' ? '인젝션' : seg.label);
    // 기밀 문장을 풀어도 안쪽 항목은 자기 상태대로
    return seg.children ? render(seg.children) : seg.original;
  }).join('');
  return render(segments);
}

/** 원문 + 탐지 결과 + 해제 키 → 최종 문자열. SW 가 결정 시점에 쓰는 한 줄 경로. */
export function finalTextFrom(text, piiItems, injectionItems, keyPrefix, unmaskedKeys, forcedMaskItems = []) {
  return buildFinalText(buildSegments(text, piiItems, injectionItems, keyPrefix, forcedMaskItems), unmaskedKeys);
}
