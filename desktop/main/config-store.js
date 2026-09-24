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
 *   - mcpRiskScannerEnabled: bool          (선택형 별도 stdio MCP 사이드카, 기본 OFF)
 *   - pipelineLayout: {nodeId: {x,y}}      (처리현황 노드 드래그 배치, PLAN §8)
 *   - detector 선택 항목은 없다. 엔진 registry 가 종류별 구현을 하나씩만 들고 있어
 *     (pii: encoder, injection: llm_mcp) 고를 대상이 없고, 실제로 예전의
 *     piiDetector/injectionDetector 는 저장·전달되면서도 엔진이 읽지 않는 값이었다.
 *     가중치가 아직 없는 동안은 엔진의 model_status 게이트가 검사 없이 통과시킨다
 *     (§PLAN 9.2). main.js 의 ensureModelsAutoDownload 가 가중치만 자동으로 내려받고,
 *     엔진 재시작은 필요 없다(다음 실제 검사 요청에서 detector 가 실 모델을 스폰한다).
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
  // 대상 MCP 동작을 차단하지 않는 선택형 검사기. 아직 설정 UI에는 노출하지 않고,
  // 저장 설정/환경 경계만 둔다. false 면 검사기 프로세스 자체를 띄우지 않는다.
  mcpRiskScannerEnabled: false,
  pipelineLayout: {}, // 처리현황 화면 노드 배치 (PLAN §8 드래그 저장)
  gpuResidency: { pii: 'always', injection: 'idle_unload', idleTimeoutMin: 10 },
  // 프록시 토글(proxy-toggle.js). proxySystemPrevious 는 우리가 시스템 프록시를 덮어쓰기
  // **전의** 값이다 — null 이면 안 덮어썼다. 앱이 강제 종료돼도 다음 실행 때 이걸로 되돌린다.
  proxyEnabled: false,
  proxySystemPrevious: null,
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

  // piiDetector / injectionDetector 는 더 두지 않는다. 엔진 registry 가 그 값을
  // 읽지 않아(구현이 종류별로 하나씩뿐) 고를 수 있는 값이 아니었고, 화면에도 고르는
  // UI 가 없다. 예전 설치의 settings.json 에 남아 있어도 아무도 읽지 않으므로
  // 'rule_based' 승격 마이그레이션도 함께 뺐다 — 그 마이그레이션이 막으려던 "엔진이
  // 알 수 없는 이름으로 기동 실패" 자체가 더는 일어나지 않는다.

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
