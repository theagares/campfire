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

test('맥 키체인 거부로 복호화가 실패하면 덮어쓰기 대신 다시 승인하는 법을 알린다', () => {
  const dir = tempDir();
  new ForcedMaskStore(dir, fakeSafeStorage()).replace(['Project Aurora']);
  const denied = { ...fakeSafeStorage(), decryptString: () => { throw new Error('Error while decrypting'); } };
  const platform = Object.getOwnPropertyDescriptor(process, 'platform');
  Object.defineProperty(process, 'platform', { value: 'darwin' });
  try {
    const store = new ForcedMaskStore(dir, denied);
    assert.equal(store.hasStoredTerms(), true);
    assert.throws(() => store.list(), (err) => err instanceof ForcedMaskStoreError
      && /키체인/.test(err.message) && /항상 허용/.test(err.message));
  } finally {
    Object.defineProperty(process, 'platform', platform);
  }
  assert.equal(new ForcedMaskStore(tempDir(), denied).hasStoredTerms(), false, '저장된 단어가 없으면 키체인을 건드리지 않는다');
});

test('맥 키체인 거부 뒤 보안 저장소 자체를 못 쓸 때도 맥 안내를 주고, 파일은 그대로 둔다', () => {
  // 맥 실측: 거부 직후엔 복호화 실패가 아니라 isEncryptionAvailable() 부터 false 라서, 위 분기에
  // 닿지 못하고 일반 문구 + "다시 입력해 덮어쓰기" 안내가 떴다.
  const dir = tempDir();
  new ForcedMaskStore(dir, fakeSafeStorage()).replace(['Project Aurora']);
  const before = fs.readFileSync(path.join(dir, 'forced-mask-terms.enc'));
  const platform = Object.getOwnPropertyDescriptor(process, 'platform');
  Object.defineProperty(process, 'platform', { value: 'darwin' });
  try {
    const store = new ForcedMaskStore(dir, fakeSafeStorage(false));
    const keychain = (err) => err instanceof ForcedMaskStoreError
      && /키체인/.test(err.message) && /항상 허용/.test(err.message);
    assert.throws(() => store.list(), keychain);
    assert.throws(() => store.replace(['새 단어']), keychain);
  } finally {
    Object.defineProperty(process, 'platform', platform);
  }
  assert.deepEqual(fs.readFileSync(path.join(dir, 'forced-mask-terms.enc')), before, '저장된 파일을 덮어썼다');
  // 맥이 아니면 예전 문구 그대로. 플랫폼을 고정해야 한다 — 맥 CI 러너에서는 실제 플랫폼이
  // darwin 이라 맥 문구가 나와 실패했다(v0.2.36 첫 빌드).
  Object.defineProperty(process, 'platform', { value: 'win32' });
  try {
    assert.throws(() => new ForcedMaskStore(dir, fakeSafeStorage(false)).list(), /운영체제 보안 저장소/);
  } finally {
    Object.defineProperty(process, 'platform', platform);
  }
});
