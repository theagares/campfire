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
 *     설정 화면에서 수동으로도 바꿀 수 있지만, 이제는 advanced(encoder/llm_mcp)가
 *     기본 목표 상태다 — main.js 의 자동 설치 루틴이 최초 실행 시 rule_based 로
 *     한 번 기동한 뒤(가중치 없이도 항상 뜨는 안전한 상태) 조용히 가중치를 내려받아
 *     advanced 로 자동 전환한다. GPU/CUDA 가 없어 advanced 기동이 실패하면 같은
 *     루틴이 rule_based 로 자동 복귀시킨다(브릭 방지).
 *     (과거엔 advancedAutoSetupDone 플래그로 "이미 한 번 했음"을 기억해 재확인을
 *     건너뛰었는데, 이 플래그는 userData 에 남아 재설치를 해도 안 지워지는 반면
 *     실제 가중치 파일은 resources/engine 에 있어 재설치하면 다시 사라진다 — 그래서
 *     재설치 후 advanced 로 설정은 돼 있는데 가중치는 없는 상태가 재현됐다. 지금은
 *     플래그 없이 매번 실제 /models/status 로 확인한다.)
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
  // 최초 spawn 은 항상 rule_based 로 시작한다(가중치가 아직 없어도 100% 뜨는 안전한
  // 상태) — main.js 의 자동 설치 루틴이 여기서 advanced 로 승격시킨다.
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
