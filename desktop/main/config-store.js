'use strict';
/**
 * main/config-store.js
 * 앱 설정 영속화 (Electron userData 에 JSON). 비밀값 저장 금지.
 *
 * 저장 항목 (PLAN §8 설정 팝업 v1):
 *   - injectionPolicy: 'mask' | 'block'   (엔진 spawn env 로 반영)
 *   - remoteUrl: string                    (익스텐션엔 없고 앱에만 존재, PLAN §3)
 *   - securityEnabled: bool                (트레이 ON/OFF = 엔진 가동 여부)
 *   - pipelineLayout: {nodeId: {x,y}}      (처리현황 노드 드래그 배치, PLAN §8)
 *   - piiDetector/injectionDetector: 저장값이 엔진 spawn env 로 반영됨(engine-manager.js).
 *     다만 값을 바꿀 UI 컨트롤은 아직 없음.
 *   - gpu 항목: v1 no-op → UI 에서 비활성. 저장은 하되 엔진에 반영 안 함.
 */

const fs = require('fs');
const path = require('path');
const constants = require('./constants');

const DEFAULTS = {
  injectionPolicy: constants.INJECTION_POLICY_DEFAULT,
  remoteUrl: constants.DEFAULT_REMOTE_URL,
  securityEnabled: true,
  pipelineLayout: {}, // 처리현황 화면 노드 배치 (PLAN §8 드래그 저장)
  // 엔진 spawn env(SECUREDOC_PII_DETECTOR/SECUREDOC_INJECTION_DETECTOR)로 반영됨
  // (engine-manager.js). 다만 이 값을 바꿀 UI 컨트롤은 아직 없고, 설치 파일에
  // encoder/llm_mcp 가 쓸 모델 가중치도 아직 번들되지 않아 실질적으로는 기본값
  // rule_based 로만 동작한다(MODELS.md 참고).
  piiDetector: 'rule_based',
  injectionDetector: 'rule_based',
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
