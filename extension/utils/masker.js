/**
 * masker.js
 * 탐지된 PII / 인젝션 위치 정보를 받아 텍스트를 마스킹하는 유틸리티
 */

const TYPE_LABELS = {
  // PII
  PERSON_NAME:   '이름',
  EMAIL:         '이메일',
  PHONE:         '전화번호',
  ADDRESS:       '주소',
  ID_NUMBER:     '신분증번호',
  CREDIT_CARD:   '카드번호',
  DATE_OF_BIRTH: '생년월일',
  ORGANIZATION:  '기관명',
  BANK_ACCOUNT:  '계좌번호',
  OTHER_PII:     '개인정보',
  // PII (로컬 encoder 모델, chan/pii TARGET_LABELS)
  PS_NAME:             '이름',
  LC_ADDRESS:           '주소',
  OG_WORKPLACE:         '근무지',
  OG_DEPARTMENT:        '부서명',
  CV_POSITION:          '직위',
  OGG_EDUCATION:        '학력',
  QT_MOBILE:            '휴대폰번호',
  QT_PHONE:             '전화번호',
  QT_RESIDENT_NUMBER:   '주민등록번호',
  QT_ALIEN_NUMBER:      '외국인등록번호',
  QT_DRIVER_NUMBER:     '운전면허번호',
  QT_PLATE_NUMBER:      '차량번호',
  QT_ACCOUNT_NUMBER:    '계좌번호',
  QT_CARD_NUMBER:       '카드번호',
  TMI_EMAIL:            '이메일',
  QT_PASSPORT_NUMBER:   '여권번호',
  QT_AGE:               '나이',
  DT_BIRTH:             '생년월일',
  FD_MAJOR:             '전공',
  // Injection
  INSTRUCTION_OVERRIDE: '명령 재정의',
  ROLE_MANIPULATION:    '역할 조작',
  SYSTEM_PROMPT_LEAK:   '시스템 프롬프트 유출',
  JAILBREAK:            '탈옥 시도',
  HIDDEN_COMMAND:       '숨겨진 명령',
  DATA_EXFILTRATION:    '데이터 유출 시도',
  OTHER_INJECTION:      '인젝션',
};

function getLabel(type) {
  return TYPE_LABELS[type] ?? type;
}

/**
 * items 목록의 위치가 실제 텍스트와 일치하는지 검증하고 보정합니다.
 * LLM이 off-by-one 오류를 낼 수 있으므로 텍스트 매칭으로 보정합니다.
 */
function validateAndFix(text, items) {
  const valid = [];
  for (const item of items) {
    const { start, end, text: itemText } = item;

    // 범위 기본 검증
    if (
      typeof start !== 'number' ||
      typeof end !== 'number' ||
      start < 0 ||
      end > text.length ||
      start >= end
    ) {
      // 텍스트 검색으로 위치 복구 시도
      const idx = text.indexOf(itemText);
      if (idx !== -1) {
        valid.push({ ...item, start: idx, end: idx + itemText.length });
      }
      continue;
    }

    // 위치가 실제 텍스트와 일치하는지 확인
    const slice = text.slice(start, end);
    if (slice === itemText) {
      valid.push(item);
    } else {
      // 약간의 오차 허용 (±5자 범위 탐색)
      let found = false;
      for (let offset = -5; offset <= 5; offset++) {
        const s = start + offset;
        const e = end + offset;
        if (s >= 0 && e <= text.length && text.slice(s, e) === itemText) {
          valid.push({ ...item, start: s, end: e });
          found = true;
          break;
        }
      }
      if (!found) {
        // 전체 검색으로 위치 복구 시도
        const idx = text.indexOf(itemText);
        if (idx !== -1) {
          valid.push({ ...item, start: idx, end: idx + itemText.length });
        }
      }
    }
  }
  return valid;
}

/**
 * 텍스트에서 item 범위들이 겹치지 않도록 병합합니다.
 * 겹치는 범위는 가장 큰 범위로 통합합니다.
 */
function mergeOverlapping(items) {
  if (items.length === 0) return [];
  const sorted = [...items].sort((a, b) => a.start - b.start);
  const merged = [sorted[0]];
  for (let i = 1; i < sorted.length; i++) {
    const last = merged[merged.length - 1];
    const cur = sorted[i];
    if (cur.start < last.end) {
      // 겹침: end를 더 큰 값으로 확장
      last.end = Math.max(last.end, cur.end);
      last.text = items[0].text; // 대표 텍스트 유지
    } else {
      merged.push(cur);
    }
  }
  return merged;
}

/**
 * 텍스트에 마스킹을 적용합니다.
 *
 * @param {string} text           - 원본 텍스트
 * @param {Array}  items          - [{type, text, start, end, ...}]
 * @param {'PII'|'INJECTION'} category
 * @returns {{ maskedText: string, applied: Array }}
 *   applied: 실제 적용된 마스킹 목록 (start/end는 maskedText 기준)
 */
export function applyMasking(text, items, category = 'PII') {
  const validated = validateAndFix(text, items);
  const unique = mergeOverlapping(validated);

  // 뒤에서부터 처리하면 앞쪽 인덱스가 유지됨
  const sorted = [...unique].sort((a, b) => b.start - a.start);

  let result = text;
  const applied = [];

  for (const item of sorted) {
    const label = getLabel(item.type);
    const placeholder = `[${label} 마스킹]`;
    result = result.slice(0, item.start) + placeholder + result.slice(item.end);
    applied.push({ ...item, placeholder, category });
  }

  return { maskedText: result, applied: applied.reverse() };
}

/**
 * 텍스트에서 마스킹된 구간을 강조한 HTML을 생성합니다.
 * (HITL 모달의 "마스킹된 내용" 패널 렌더링용)
 *
 * @param {string} text    - 마스킹된 텍스트
 * @param {'light'|'dark'} theme
 * @returns {string} HTML string
 */
export function buildMaskedHTML(text) {
  // [xxx 마스킹] 패턴을 하이라이트 span으로 변환
  const escaped = escapeHtml(text);
  return escaped.replace(
    /\[([^\]]+) 마스킹\]/g,
    (match, label) => {
      const isPii = !['명령 재정의', '역할 조작', '시스템 프롬프트 유출',
                      '탈옥 시도', '숨겨진 명령', '데이터 유출 시도', '인젝션']
                    .includes(label);
      const cls = isPii ? 'mask-pii' : 'mask-injection';
      return `<span class="mask-tag ${cls}">[${label} 마스킹]</span>`;
    }
  );
}

/**
 * 원본 텍스트에서 PII/인젝션 구간을 강조한 HTML을 생성합니다.
 * (HITL 모달의 "원본 내용" 패널 렌더링용)
 *
 * @param {string} text
 * @param {Array}  piiItems       - PII 탐지 결과
 * @param {Array}  injectionItems - 인젝션 탐지 결과
 * @returns {string} HTML string
 */
export function buildOriginalHTML(text, piiItems, injectionItems) {
  // 모든 span을 합쳐 정렬
  const allItems = [
    ...piiItems.map(i => ({ ...i, category: 'PII' })),
    ...injectionItems.map(i => ({ ...i, category: 'INJECTION' })),
  ].sort((a, b) => a.start - b.start);

  const merged = mergeOverlapping(allItems);
  let html = '';
  let cursor = 0;

  for (const item of merged) {
    if (item.start > cursor) {
      html += escapeHtml(text.slice(cursor, item.start));
    }
    const cls = item.category === 'PII' ? 'highlight-pii' : 'highlight-injection';
    const label = getLabel(item.type);
    html += `<mark class="${cls}" title="${label}">${escapeHtml(text.slice(item.start, item.end))}</mark>`;
    cursor = item.end;
  }

  if (cursor < text.length) {
    html += escapeHtml(text.slice(cursor));
  }

  return html;
}

function escapeHtml(str) {
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
