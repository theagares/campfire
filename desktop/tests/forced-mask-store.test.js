'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {
  ForcedMaskStore, ForcedMaskStoreError, normalizeTerms, MAX_TERM_CHARS,
} = require('../main/forced-mask-store');

function fakeSafeStorage(available = true) {
  return {
    isEncryptionAvailable: () => available,
    encryptString: (value) => Buffer.from(`encrypted:${Buffer.from(value).toString('base64')}`),
    decryptString: (value) => Buffer.from(
      value.toString().replace(/^encrypted:/, ''), 'base64',
    ).toString(),
  };
}

function tempDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'campfire-forced-mask-'));
}

test('단어는 정규화·중복 제거 후 암호화 저장된다', () => {
  const dir = tempDir();
  const store = new ForcedMaskStore(dir, fakeSafeStorage());
  assert.deepEqual(store.replace([' Project Aurora ', 'project aurora', '기밀']), ['Project Aurora', '기밀']);
  const raw = fs.readFileSync(store.filePath, 'utf8');
  assert.equal(raw.includes('Project Aurora'), false);
  assert.deepEqual(new ForcedMaskStore(dir, fakeSafeStorage()).list(), ['Project Aurora', '기밀']);
  assert.deepEqual(store.replace(['교체된 규칙']), ['교체된 규칙'], '기존 암호문을 원자적으로 교체');
  assert.deepEqual(store.list(), ['교체된 규칙']);
});

test('빈 항목과 중복은 제거하고 제한을 검증한다', () => {
  assert.deepEqual(normalizeTerms(['', ' Secret ', 'secret']), ['Secret']);
  assert.throws(() => normalizeTerms(['x'.repeat(MAX_TERM_CHARS + 1)]), ForcedMaskStoreError);
  assert.throws(() => normalizeTerms(['line\nbreak']), ForcedMaskStoreError);
});

test('OS 암호화를 사용할 수 없으면 평문 폴백하지 않는다', () => {
  const store = new ForcedMaskStore(tempDir(), fakeSafeStorage(false));
  assert.throws(() => store.replace(['secret']), ForcedMaskStoreError);
  assert.equal(fs.existsSync(store.filePath), false);
});

test('손상된 암호문은 빈 정책으로 조용히 바꾸지 않는다', () => {
  const dir = tempDir();
  const store = new ForcedMaskStore(dir, fakeSafeStorage());
  fs.writeFileSync(store.filePath, 'broken');
  assert.throws(() => store.list(), ForcedMaskStoreError);
});
