// status-classify.test.mjs (node --test)
//
// 이 판정이 틀리면 라이브-QA 가 (a) 원본 유출을 놓치거나 (b) 실사용 멀쩡한 사이트로
// CI 를 상시 실패시킨다. 그래서 "유출은 언제나 하드 실패", "봇/지역/fail-closed 는
// 경고(CI 안 깸)", "검증됨은 통과" 세 축을 못 박는다.

import test from 'node:test';
import assert from 'node:assert';
import { isHardFail, statusMark } from './status-classify.mjs';

test('유출(failed-leak)은 run·preflight 어디서든 하드 실패', () => {
  assert.equal(isHardFail('run', 'failed-leak'), true);
  assert.equal(isHardFail('preflight', 'failed-leak'), true);
  assert.equal(statusMark('run', 'failed-leak'), 'LEAK-FAIL');
});

test('검증된 상태는 통과(하드 실패 아님)', () => {
  for (const s of ['upload-response-ok', 'content-response-ok']) {
    assert.equal(isHardFail('run', s), false);
    assert.equal(statusMark('run', s), 'ok');
  }
});

test('자동 검증 불가/ fail-closed 는 경고 — CI 안 깸', () => {
  for (const s of ['human-verification-needed', 'region-blocked', 'upload-control-needed',
    'provider-upload-limit', 'attachment-reinject-failed']) {
    assert.equal(isHardFail('run', s), false, s);
    assert.equal(statusMark('run', s), 'blocked(manual)', s);
  }
});

test('우리 쪽 문제로 보이는 미분류 상태는 하드 실패로 남긴다', () => {
  for (const s of ['login-or-selector-needed', 'inconclusive', 'masked-input-only']) {
    assert.equal(isHardFail('run', s), true, s);
    assert.equal(statusMark('run', s), 'FAIL', s);
  }
});

test('preflight: ready 는 통과, 외부 차단은 경고, 그 외는 실패', () => {
  assert.equal(isHardFail('preflight', 'ready'), false);
  assert.equal(statusMark('preflight', 'ready'), 'ok');
  assert.equal(isHardFail('preflight', 'human-verification-needed'), false);
  assert.equal(isHardFail('preflight', 'region-blocked'), false);
  assert.equal(isHardFail('preflight', 'login-or-selector-needed'), true); // 셀렉터 깨짐 = 우리 탓
});
