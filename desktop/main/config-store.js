'use strict';
/**
 * main/config-store.js
 * 앱 설정 영속화 (Electron userData 에 JSON). 비밀값 저장 금지가 원칙이나,
 * upstageApiKey 는 예외다 — 사용자 본인의 Upstage Solar API 키를 로컬에 저장하는
 * 것 외에 다른 실용적 방법이 없어 의도적으로 예외 처리했다. 이 값은 암호화되지
 * 않은 평문 JSON(settings.json)에 그대로 저장된다는 점에 유의할 것.
 *
 * 저장 항목 (PLAN §8 설정 팝업 v1):
 *   - injectionPolicy: 'mask' | 'block'   (엔진 spawn env 로 반영)
 *   - remoteUrl: string                    (익스텐션엔 없고 앱에만 존재, PLAN §3)
 *   - upstageApiKey: string                (Upstage Solar Pro API 키. 인젝션 탐지 2단계
 *     localize 에 사용됨 — engine/app/core/detectors/injection/llm_mcp.py 의
 *     _localize_with_solar(). 비어 있으면 엔진이 INJECTION_LOCALIZE_ENABLED=False 로
 *     동작해 인젝션 청크 전체를 마스킹하는 fail-safe 로 빠진다. 평문 저장(비암호화)임에
 *     유의 — settings.json 은 암호화되지 않는다.)
 *   - securityEnabled: bool                (트레이 ON/OFF = 엔진 가동 여부)
 *   - pipelineLayout: {nodeId: {x,y}}      (처리현황 노드 드래그 배치, PLAN §8)
 *   - piiDetector/injectionDetector: 저장값이 엔진 spawn env 로 반영됨(engine-manager.js).
 *     엔진에서 룰베이스 폴백을 완전히 제거한 뒤에는 encoder/llm_mcp 가 유일한 값이라
 *     사실상 고정 상수다(설정 화면의 선택 UI도 없앴다) — 가중치가 아직 없는 동안은
 *     엔진의 model_status 게이트가 검사 없이 통과시킨다(§PLAN 9.2). main.js 의
 *     ensureModelsAutoDownload 가 가중치만 자동으로 내려받고, 엔진 재시작은 필요 없다
 *     (다음 실제 검사 요청에서 detector 가 알아서 실 모델을 스폰한다).
 *     예전 설치에서 저장된 'rule_based' 값은 _load()가 encoder/llm_mcp 로 자동
 *     승격시킨다(아래 _migrateRemovedRuleBased) — 안 그러면 엔진이 알 수 없는
 *     detector 이름으로 기동 실패한다.
 *   - gpu 항목: v1 no-op → UI 에서 비활성. 저장은 하되 엔진에 반영 안 함.
 */

const fs = require('fs');
const path = require('path');
const constants = require('./constants');

const DEFAULTS = {
  injectionPolicy: constants.INJECTION_POLICY_DEFAULT,
  remoteUrl: constants.DEFAULT_REMOTE_URL,
  // Upstage Solar API 키(평문 저장, settings.json 비암호화). 비어 있으면 엔진이
  // 인젝션 localize 를 못 하고 청크 전체 마스킹 fail-safe 로 동작한다.
  upstageApiKey: '',
  securityEnabled: true,
  pipelineLayout: {}, // 처리현황 화면 노드 배치 (PLAN §8 드래그 저장)
  // 룰베이스 폴백 제거 후 유일한 값 — 가중치가 없어도 엔진 자체는 정상 기동하고,
  // 검사 시점에 model_status 게이트가 통과 처리한다(§PLAN 9.2).
  piiDetector: 'encoder',
  injectionDetector: 'llm_mcp',
  gpuResidency: { pii: 'always', injection: 'idle_unload', idleTimeoutMin: 10 },
};

class ConfigStore {
  constructor(userDataDir) {
    this.filePath = path.join(userDataDir, 'settings.json');
    this.data = { ...DEFAULTS };
    this._load();
  }

  _load() {
    try {
      const raw = fs.readFileSync(this.filePath, 'utf-8');
      const parsed = JSON.parse(raw);
      this.data = { ...DEFAULTS, ...parsed };
    } catch {
      // 파일 없음/파싱 실패 → 기본값 사용
      this.data = { ...DEFAULTS };
    }
    this._migrateRemovedRuleBased();
  }

  /** 예전 설치에서 저장된 'rule_based' 값을 encoder/llm_mcp 로 승격한다 — 엔진에서
   * 룰베이스를 완전히 없앴으므로, 이 값을 그대로 spawn env 에 실으면 엔진이 알 수
   * 없는 detector 이름으로 기동에 실패한다. */
  _migrateRemovedRuleBased() {
    let changed = false;
    if (this.data.piiDetector === 'rule_based') { this.data.piiDetector = 'encoder'; changed = true; }
    if (this.data.injectionDetector === 'rule_based') { this.data.injectionDetector = 'llm_mcp'; changed = true; }
    if (changed) this._save();
  }

  _save() {
    try {
      fs.mkdirSync(path.dirname(this.filePath), { recursive: true });
      fs.writeFileSync(this.filePath, JSON.stringify(this.data, null, 2), 'utf-8');
    } catch (err) {
      console.error('[config-store] 저장 실패:', err.message);
    }
  }

  get(key) {
    return key ? this.data[key] : { ...this.data };
  }

  set(patch) {
    this.data = { ...this.data, ...patch };
    this._save();
    return this.get();
  }

  setPipelineLayout(layout) {
    this.data.pipelineLayout = layout || {};
    this._save();
  }
}

module.exports = { ConfigStore, DEFAULTS };
