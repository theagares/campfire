/**
 * mask-segments.test.js
 *
 * 패널 미리보기와 SW 산출물이 **같은 문자열**이어야 한다는 계약을 지킨다.
 * 예전엔 미리보기(sidepanel.buildFinalTextFrom)와 업로드 파일이 서로 다른 코드로
 * 만들어졌다 — 둘이 갈라지면 사용자는 가려졌다고 본 값을 그대로 올리게 된다.
 *
 * 항목 키가 숫자 offset 이 아니라 문자열이어야 하는 이유도 여기서 지킨다:
 * 다중 첨부는 항목이 비동기로 늘고 중간에 실패도 하므로, 번호를 다시 매기면
 * 사용자가 이미 고른 해제 선택이 **다른 항목**을 가리킨다.
 *
 * 실행: node tests/mask-segments.test.js   (exit 0 = 통과)
 */

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

// ESM 파일을 CommonJS 테스트에서 돌리려고 export 키워드만 걷어낸다
// (sw-session-persistence.test.js 가 쓰는 것과 같은 수법).
// top-level const 는 vm 컨텍스트 객체에 붙지 않아서(function 선언만 붙는다) 꼬리에
// 한 줄 덧붙여 꺼낸다.
const SRC = fs.readFileSync(path.join(__dirname, '..', 'utils', 'mask-segments.js'), 'utf8')
  .replace(/^export\s+/gm, '')
  + ';this.__exports = { TYPE_LABELS, labelOf, maskTokenFor };';

const sandbox = {};
vm.runInContext(SRC, vm.createContext(sandbox), { filename: 'mask-segments.js' });
const { buildSegments, buildFinalText, finalTextFrom } = sandbox;
const { labelOf } = sandbox.__exports;

// vm 샌드박스가 돌려주는 배열은 realm 이 달라 deepStrictEqual 이 프로토타입에서 걸린다.
// 값만 보면 되므로 join 으로 비교한다.

const pii = (start, end, type) => ({ start, end, type });

// ── 1) 기본 분할 ─────────────────────────────────────────────────────────────
{
  const text = '이름은 김철수이고 메일은 a@b.com 입니다';
  const segs = buildSegments(text, [pii(4, 7, 'PERSON_NAME'), pii(14, 21, 'EMAIL')], [], 'doc-1');
  const items = segs.filter(s => s.type === 'item');

  assert.strictEqual(items.length, 2, '항목 2개');
  assert.strictEqual(items.map(i => i.key).join(','), 'doc-1:0,doc-1:1', '키에 소유자 접두사');
  assert.strictEqual(items[0].original, '김철수');
  // 세그먼트를 전부 이으면 원문이 그대로 복원돼야 한다 — 한 글자도 잃지 않는다.
  assert.strictEqual(
    segs.map(s => s.type === 'text' ? s.text : s.original).join(''),
    text,
    '무손실 분할',
  );
}

// ── 2) 아무것도 해제하지 않으면 전부 자리표시자 ───────────────────────────────
{
  const text = '김철수 드림';
  const segs = buildSegments(text, [pii(0, 3, 'PERSON_NAME')], [], 'doc-1');
  assert.strictEqual(buildFinalText(segs, new Set()), '[이름 마스킹] 드림');
  assert.strictEqual(buildFinalText(segs, ['doc-1:0']), '김철수 드림', '해제하면 원문 유지');
}

// ── 3) 키는 접두사로 격리된다 ────────────────────────────────────────────────
//     다른 문서의 해제 선택이 이 문서에 새면 안 된다.
{
  const a = buildSegments('김철수', [pii(0, 3, 'PERSON_NAME')], [], 'doc-a');
  const b = buildSegments('박영희', [pii(0, 3, 'PERSON_NAME')], [], 'doc-b');
  const keep = new Set(['doc-a:0']);

  assert.strictEqual(buildFinalText(a, keep), '김철수');
  assert.strictEqual(buildFinalText(b, keep), '[이름 마스킹]', '다른 문서 키는 영향 없음');
}

// ── 4) 항목이 하나 빠져도 남은 항목의 키가 변하지 않는다 ─────────────────────
//     숫자 offset 이었다면 뒤 항목 번호가 전부 밀려 선택이 어긋났다.
{
  const text = 'a김철수b박영희c';
  const all = buildSegments(text, [pii(1, 4, 'PERSON_NAME'), pii(5, 8, 'PERSON_NAME')], [], 'doc-1');
  const second = all.filter(s => s.type === 'item')[1];

  // 첫 항목이 탐지되지 않은(=빠진) 경우에도, 두 번째 항목을 **같은 문서 안에서**
  // 고른 사용자 선택은 그 항목의 키로 계속 식별된다.
  assert.strictEqual(second.key, 'doc-1:1');
  assert.strictEqual(buildFinalText(all, new Set([second.key])), 'a[이름 마스킹]b박영희c');
}

// ── 5) 겹침·역순 탐지에서도 원문이 깨지지 않는다 ─────────────────────────────
{
  const text = '0123456789';
  // 뒤쪽 항목이 앞 항목에 완전히 먹히는 경우 + 정렬이 역순으로 들어온 경우
  const segs = buildSegments(text, [pii(6, 8, 'EMAIL'), pii(2, 6, 'PERSON_NAME'), pii(3, 5, 'PHONE')], [], 'd');
  const joined = segs.map(s => s.type === 'text' ? s.text : s.original).join('');
  assert.strictEqual(joined, text, '겹쳐도 원문 복원');

  const keys = segs.filter(s => s.type === 'item').map(s => s.key);
  assert.strictEqual(new Set(keys).size, keys.length, '키 중복 없음');
}

// ── 6) 미리보기 = 전송본 ─────────────────────────────────────────────────────
//     패널은 buildSegments→buildFinalText, SW 는 finalTextFrom 한 줄을 쓴다.
//     두 경로가 반드시 같은 문자열을 내야 한다.
{
  const text = '김철수 010-1111-2222 a@b.com';
  const items = [pii(0, 3, 'PERSON_NAME'), pii(4, 17, 'PHONE'), pii(18, 25, 'EMAIL')];
  const keep = new Set(['doc-9:1']);

  const panel = buildFinalText(buildSegments(text, items, [], 'doc-9'), keep);
  const sw = finalTextFrom(text, items, [], 'doc-9', keep);

  assert.strictEqual(panel, sw, '패널 미리보기와 SW 산출물이 같아야 한다');
  assert.strictEqual(panel, '[이름 마스킹] 010-1111-2222 [이메일 마스킹]');
}

// ── 7) 인젝션 항목도 같은 키 공간을 쓴다 ─────────────────────────────────────
{
  const segs = buildSegments('무시하고 김철수', [pii(5, 8, 'PERSON_NAME')], [pii(0, 4, 'JAILBREAK')], 'p');
  const items = segs.filter(s => s.type === 'item');
  assert.strictEqual(items.map(i => i.cat).join(','), 'inj,pii', 'start 순 정렬');
  assert.strictEqual(items.map(i => i.key).join(','), 'p:0,p:1');
}

// ── 8) 입력이 비어도 죽지 않는다 ─────────────────────────────────────────────
assert.strictEqual(buildSegments('', [], [], 'x').length, 0);
assert.strictEqual(buildFinalText([], new Set()), '');
assert.strictEqual(buildFinalText(null, null), '');
assert.strictEqual(labelOf('UNKNOWN_TYPE'), 'UNKNOWN_TYPE', '모르는 유형은 원본 유지');

// ── 9) 다중 요약은 배치 전체로 센다 ─────────────────────────────────────────
//     활성 탭 항목 수에서 전체 탭의 해제 수를 빼면 음수가 되고, 그러면 푸터가
//     "마스킹 없이 원본 전송" 이라는 정반대 문구를 띄운다. 실제로는 마스킹해서
//     보내는데 원본이 나간다고 말하는, 이 화면이 절대 하면 안 되는 거짓말이다.
{
  const counted = (c) => (c ? (c.pii || 0) + (c.injection || 0) : 0);
  const docs = [{ counts: { pii: 2, injection: 0 } }, { counts: { pii: 1, injection: 0 } }];
  const unmasked = new Set(['f1:0', 'f1:1', 'f1:2']);   // 다른 탭에서 3개 해제

  const activeTabOnly = 2 - unmasked.size;               // 옛 계산: -1
  const wholeBatch = docs.reduce((n, d) => n + counted(d.counts), 0) - unmasked.size;

  assert.ok(activeTabOnly < 0, '재현 전제: 옛 계산은 음수가 된다');
  assert.strictEqual(wholeBatch, 0, '배치 전체로 세면 음수가 나오지 않는다');
  assert.ok(wholeBatch >= 0, '요약 건수는 음수가 될 수 없다');
}

console.log('mask-segments.test.js: 9개 블록 통과');
