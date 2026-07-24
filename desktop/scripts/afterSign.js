'use strict';
/**
 * scripts/afterSign.js — electron-builder afterSign 훅 (build.afterSign, package.json 참고).
 * "run after pack and sign, but before pack into distributable format" — electron-builder
 * 자신의 서명 시도가 끝난 직후, dmg 로 패키징되기 전에 실행된다.
 *
 * mac.identity 설정은 electron-builder 안에서 "키체인에서 이 문자열을 포함하는 인증서를
 * 찾아라"는 검색어로만 쓰인다 — 진짜 Apple Developer 인증서가 없으면(이 프로젝트가 그렇다)
 * "-"를 넣어도 ad-hoc 서명을 강제하지 못하고 그냥 서명 없이 넘어간다(실측 확인:
 * identity:"-" 를 넣어도 spctl 이 여전히 "rejected/no usable signature"). Apple Silicon
 * (arm64) 은 서명이 아예 없는 바이너리는 커널이 실행 자체를 거부하므로, 여기서 codesign 을
 * 직접 호출해 ad-hoc(--sign -) 서명을 강제한다. 유료 인증서 없이도 가능하고, 이 정도만
 * 있어도 Gatekeeper 가 실행 자체는 허용한다(단, 노터라이즈가 아니므로 최초 실행 시
 * "확인되지 않은 개발자" 경고는 뜰 수 있음 — 사용자가 우클릭 열기로 넘기면 됨).
 */

const path = require('path');
const { execFileSync } = require('child_process');

module.exports = async function afterSign(context) {
  if (context.electronPlatformName !== 'darwin') return;

  const appName = `${context.packager.appInfo.productFilename}.app`;
  const appPath = path.join(context.appOutDir, appName);

  console.log(`[afterSign] ad-hoc 코드사이닝: ${appPath}`);
  execFileSync('codesign', ['--sign', '-', '--deep', '--force', '--timestamp=none', appPath], {
    stdio: 'inherit',
  });
};
