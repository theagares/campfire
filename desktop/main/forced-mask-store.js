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

// 맥은 이 파일의 암호화 키가 키체인에 있다. 앱 서명이 바뀐 뒤(새 버전·정식/라이트 교체)
// 첫 실행의 승인 창에서 "거부" 하면 이 실행 동안 키를 못 받는다 — 거부 직후엔 복호화 실패가
// 아니라 isEncryptionAvailable() 부터 false 다(맥 실측). 파일은 멀쩡하므로 덮어쓰기보다 다시
// 승인할 길을 먼저 알린다. 렌더러는 이 문구의 "키체인" 을 보고 덮어쓰기 안내를 뺀다.
function macKeychainError(detail) {
  return new ForcedMaskStoreError(
    `macOS 키체인에서 암호화 키를 받지 못했습니다(${detail}). 승인 창에서 거부했다면 앱을 종료했다가 다시 열고, 'Campfire Safe Storage' 승인 창에서 로그인 암호를 입력한 뒤 '항상 허용'을 누르세요. 저장된 단어는 그대로 남아 있습니다.`,
  );
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

  /** 읽으려면 복호화(맥은 키체인 접근)가 필요한가. 저장된 단어가 없으면 키체인을 건드리지 않는다. */
  hasStoredTerms() {
    return fs.existsSync(this.filePath);
  }

  list() {
    if (!fs.existsSync(this.filePath)) return [];
    if (!this._encryptionAvailable()) {
      if (process.platform === 'darwin') throw macKeychainError('보안 저장소를 쓸 수 없음');
      throw new ForcedMaskStoreError('운영체제 보안 저장소를 사용할 수 없어 단어를 불러오지 못했습니다.');
    }
    try {
      const encrypted = fs.readFileSync(this.filePath);
      let plain;
      try {
        plain = this.safeStorage.decryptString(encrypted);
      } catch (err) {
        if (process.platform !== 'darwin') throw err;
        throw macKeychainError(err.message);
      }
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
      if (process.platform === 'darwin') throw macKeychainError('보안 저장소를 쓸 수 없어 저장도 못 함');
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
