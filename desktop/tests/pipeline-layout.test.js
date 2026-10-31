'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const desktop = path.join(__dirname, '..');
const app = fs.readFileSync(path.join(desktop, 'renderer', 'app.js'), 'utf8');
const assets = path.join(desktop, 'assets', 'figma');

function literal(name) {
  const token = `const ${name} = `;
  const at = app.indexOf(token);
  assert.notEqual(at, -1, `${name} 선언이 없다`);
  const start = at + token.length;
  const open = app[start];
  const close = open === '[' ? ']' : open === '{' ? '}' : null;
  assert.ok(close, `${name}은 배열이나 객체 리터럴이어야 한다`);

  let depth = 0;
  let quote = null;
  let escaped = false;
  for (let i = start; i < app.length; i += 1) {
    const ch = app[i];
    if (quote) {
      if (escaped) escaped = false;
      else if (ch === '\\') escaped = true;
      else if (ch === quote) quote = null;
      continue;
    }
    if (ch === "'" || ch === '"' || ch === '`') { quote = ch; continue; }
    if (ch === open) depth += 1;
    if (ch === close && --depth === 0) {
      return JSON.parse(JSON.stringify(vm.runInNewContext(`(${app.slice(start, i + 1)})`)));
    }
  }
  throw new Error(`${name} 리터럴의 끝을 찾지 못했다`);
}

test('최종 시안의 9개 노드와 실선 간선을 그대로 쓴다', () => {
  const nodes = literal('PIPE_NODES');
  const edges = literal('PIPE_EDGES');

  assert.deepEqual(nodes.map((node) => node.id), [
    'receive', 'text-extract', 'ocr',
    'pii-detect', 'inj-detect', 'conf-detect',
    'pii-done', 'inj-done', 'conf-done',
  ]);
  assert.equal(nodes.find((node) => node.id === 'receive').label, '시작');
  assert.deepEqual(
    nodes.filter((node) => ['receive', 'text-extract', 'ocr'].includes(node.id))
      .map(({ id, left, top }) => ({ id, left, top })),
    [
      { id: 'receive', left: 0, top: 34 },
      { id: 'text-extract', left: 20, top: 34 },
      { id: 'ocr', left: 40, top: 34 },
    ],
  );
  assert.equal(nodes.find((node) => node.id === 'conf-detect').src, 'pii-detect');
  assert.equal(nodes.find((node) => node.id === 'ocr').src, 'varco-ocr');
  assert.equal(nodes.some((node) => node.id === 'parse'), false);

  assert.deepEqual(edges.map(({ from, to }) => `${from}>${to}`), [
    'receive>text-extract', 'text-extract>ocr',
    'ocr>pii-detect', 'ocr>inj-detect', 'ocr>conf-detect',
    'pii-detect>pii-done', 'inj-detect>inj-done', 'conf-detect>conf-done',
  ]);
  assert.equal(edges.some((edge) => edge.dashed), false);
});

test('실시간 parse 단계는 텍스트 추출과 OCR을 차례로 밝힌다', () => {
  const stageNodes = literal('PIPE_STAGE_NODES');
  const stageEdges = literal('PIPE_STAGE_EDGES');

  assert.deepEqual(stageNodes.parse, ['text-extract', 'ocr']);
  assert.deepEqual(stageEdges.parse, ['receive>text-extract', 'text-extract>ocr']);
  assert.deepEqual(stageEdges.pii, ['ocr>pii-detect']);
  assert.deepEqual(stageEdges.injection, ['ocr>inj-detect']);
  assert.deepEqual(stageEdges.confidential, ['ocr>conf-detect']);
  assert.match(app, /PIPE_STAGE_EDGES\[currentStage\][\s\S]*\.filter\(pipeEdgeVisible\)/);
});

test('판 번호가 없는 옛 끌기 좌표는 무시하고 v2 좌표만 읽는다', () => {
  const start = app.indexOf('const savedPipelinePositions = ');
  const end = app.indexOf('\n};', start);
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);

  const source = app.slice(start, end + 3);
  const context = {};
  vm.runInNewContext(`
    const PIPE_LAYOUT_VERSION = 2;
    ${source}
    result = {
      old: savedPipelinePositions({ pipelineLayout: { receive: { left: 99, top: 99 } } }),
      wrong: savedPipelinePositions({ pipelineLayout: { version: 1, nodes: { receive: { left: 88, top: 88 } } } }),
      current: savedPipelinePositions({ pipelineLayout: { version: 2, nodes: { receive: { left: 7, top: 8 } } } }),
    };
  `, context);
  const result = JSON.parse(JSON.stringify(context.result));
  assert.deepEqual(result.old, {});
  assert.deepEqual(result.wrong, {});
  assert.deepEqual(result.current, { receive: { left: 7, top: 8 } });
  assert.match(app, /savePipelineLayout\(saved\)/);
  assert.match(app, /version: PIPE_LAYOUT_VERSION, nodes: layout/);
});

test('최종 노드 자산만 패키징한다', () => {
  for (const name of [
    'pipe-node-text-extract-unified.svg',
    'pipe-node-varco-ocr.svg',
    'pipe-node-conf-done.svg',
  ]) {
    assert.equal(fs.existsSync(path.join(assets, name)), true, `${name}가 없다`);
  }
  assert.equal(fs.existsSync(path.join(assets, 'pipe-node-parse.svg')), false);
  assert.equal(fs.existsSync(path.join(assets, 'pipe-node-conf-detect.svg')), false);
});
