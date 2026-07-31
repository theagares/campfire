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
 *     설정 화면에서 수동으로도 바꿀 수 있지만, 이제는 advanced(encoder/llm_mcp)가
 *     기본 목표 상태다 — main.js 의 자동 설치 루틴이 최초 실행 시 rule_based 로
 *     한 번 기동한 뒤(가중치 없이도 항상 뜨는 안전한 상태) 조용히 가중치를 내려받아
 *     advanced 로 자동 전환한다. GPU/CUDA 가 없어 advanced 기동이 실패하면 같은
 *     루틴이 rule_based 로 자동 복귀시킨다(브릭 방지).
 *   - advancedAutoSetupDone: 위 자동 설치 루틴을 이미 (성공적으로) 한 번 수행했는지
 *     표시 — 매 실행마다 재시도하지 않기 위한 플래그. 실패 시엔 false 로 남겨 다음
 *     실행에서 다시 시도한다.
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
  // 최초 spawn 은 항상 rule_based 로 시작한다(가중치가 아직 없어도 100% 뜨는 안전한
  // 상태) — main.js 의 자동 설치 루틴이 여기서 advanced 로 승격시킨다.
  piiDetector: 'rule_based',
  injectionDetector: 'rule_based',
  advancedAutoSetupDone: false,
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
