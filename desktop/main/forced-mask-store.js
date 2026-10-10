'use strict';

/** Encrypted persistence for user-defined mandatory mask terms. */

const fs = require('fs');
const path = require('path');

const MAX_TERMS = 500;
const MAX_TERM_CHARS = 200;
const MAX_TOTAL_CHARS = 50000;

class ForcedMaskStoreError extends Error {}

function normalizeTerms(values) {
  if (!Array.isArray(values)) throw new ForcedMaskStoreError('단어 목록 형식이 올바르지 않습니다.');
  const terms = [];
  const seen = new Set();
  let total = 0;
  for (const raw of values) {
    if (typeof raw !== 'string') throw new ForcedMaskStoreError('모든 항목은 문자열이어야 합니다.');
    const term = raw.trim().normalize('NFC');
    if (!term) continue;
    if (/[\r\n\0]/u.test(term)) throw new ForcedMaskStoreError('단어에는 줄바꿈을 넣을 수 없습니다.');
    if ([...term].length > MAX_TERM_CHARS) {
      throw new ForcedMaskStoreError(`단어 하나는 ${MAX_TERM_CHARS}자까지 등록할 수 있습니다.`);
    }
    const key = term.normalize('NFD').toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    total += [...term].length;
    if (total > MAX_TOTAL_CHARS) {
      throw new ForcedMaskStoreError(`전체 단어 길이는 ${MAX_TOTAL_CHARS.toLocaleString()}자까지 가능합니다.`);
    }
    terms.push(term);
    if (terms.length > MAX_TERMS) {
      throw new ForcedMaskStoreError(`최대 ${MAX_TERMS}개까지 등록할 수 있습니다.`);
    }
  }
  return terms;
}

class ForcedMaskStore {
  constructor(userDataDir, safeStorage) {
    this.filePath = path.join(userDataDir, 'forced-mask-terms.enc');
    this.safeStorage = safeStorage;
  }

  _encryptionAvailable() {
    return !!this.safeStorage
      && typeof this.safeStorage.isEncryptionAvailable === 'function'
      && this.safeStorage.isEncryptionAvailable();
  }

  list() {
    if (!fs.existsSync(this.filePath)) return [];
    if (!this._encryptionAvailable()) {
      throw new ForcedMaskStoreError('운영체제 보안 저장소를 사용할 수 없어 단어를 불러오지 못했습니다.');
    }
    try {
      const encrypted = fs.readFileSync(this.filePath);
      const plain = this.safeStorage.decryptString(encrypted);
      const parsed = JSON.parse(plain);
      if (!parsed || parsed.version !== 1) throw new Error('unsupported version');
      return normalizeTerms(parsed.terms);
    } catch (err) {
      if (err instanceof ForcedMaskStoreError) throw err;
      throw new ForcedMaskStoreError(`암호화된 강제 마스킹 단어를 읽지 못했습니다: ${err.message}`);
    }
  }

  replace(values) {
    const terms = normalizeTerms(values);
    if (!this._encryptionAvailable()) {
      throw new ForcedMaskStoreError('운영체제 보안 저장소를 사용할 수 없어 단어를 저장하지 못했습니다.');
    }
    const encrypted = this.safeStorage.encryptString(JSON.stringify({ version: 1, terms }));
    const dir = path.dirname(this.filePath);
    const temp = `${this.filePath}.${process.pid}.${Date.now()}.tmp`;
    fs.mkdirSync(dir, { recursive: true });
    try {
      fs.writeFileSync(temp, encrypted, { mode: 0o600 });
      if (process.platform !== 'win32') fs.chmodSync(temp, 0o600);
      fs.renameSync(temp, this.filePath);
    } catch (err) {
      try { fs.rmSync(temp, { force: true }); } catch { /* best effort */ }
      throw new ForcedMaskStoreError(`강제 마스킹 단어를 저장하지 못했습니다: ${err.message}`);
    }
    return terms;
  }

  limits() {
    return { maxTerms: MAX_TERMS, maxTermChars: MAX_TERM_CHARS, maxTotalChars: MAX_TOTAL_CHARS };
  }
}

module.exports = {
  ForcedMaskStore,
  ForcedMaskStoreError,
  normalizeTerms,
  MAX_TERMS,
  MAX_TERM_CHARS,
  MAX_TOTAL_CHARS,
};
