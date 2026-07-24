'use strict';
/**
 * main/paths.js
 * 엔진(Python 사이드카) 경로 해석 — dev / prod 를 모두 지원하고, 환경변수로 재정의 가능.
 *
 * 우선순위:
 *   1) SECUREDOC_ENGINE_DIR 환경변수 (있으면 그대로 사용)
 *   2) prod(app.isPackaged): process.resourcesPath/engine  (extraResources 로 번들, package.json 참고)
 *   3) dev: <desktop>/../engine  (소스트리의 형제 폴더)
 *
 * 엔진 실행 계약 (PLAN §8, 요구사항):
 *   - cwd = <engineDir>
 *   - 명령 = <engineDir>/.venv/Scripts/python.exe -m app.main   (win, 개발용 stdlib venv 레이아웃)
 *            <engineDir>/.venv/bin/python       -m app.main   (mac/linux, 개발용 stdlib venv 레이아웃)
 *   - 엔진은 스스로 48200~48209 포트를 스캔해 바인딩하고 /health 로 실제 포트를 알린다(PLAN §11).
 *
 * 패키징된 배포본(extraResources 로 번들되는 .venv)은 위 stdlib venv 레이아웃이 아니라
 * "portable" Python(uv 가 관리하는 python-build-standalone 빌드 등, 자기 완결적이라
 * 어디로 옮겨도 그대로 실행됨) 을 그대로 복사해 넣는다 — Scripts/ 하위가 아니라 .venv
 * 루트에 python.exe(win)/bin/python(mac) 이 바로 온다. stdlib venv 는 Windows에서
 * Scripts/python.exe 가 pyvenv.cfg 의 home 경로(빌드 머신에만 있는 절대경로)를 찾는
 * 런처 스텁이라 다른 머신에 그대로 복사하면 "No Python at ..." 로 죽는다(실측 확인됨,
 * mac 은 bin/python 이 절대경로 심볼릭 링크라 동일한 문제). resolvePythonExe 는 두
 * 레이아웃을 모두 찾아보고 실제 존재하는 쪽을 쓴다.
 */

const path = require('path');
const fs = require('fs');

/** @returns {string} 엔진 루트 디렉터리 (cwd 로 사용) */
function resolveEngineDir(app) {
  const override = process.env.SECUREDOC_ENGINE_DIR;
  if (override && override.trim()) {
    return path.resolve(override.trim());
  }
  if (app && app.isPackaged) {
    // prod: extraResources 로 번들된 위치
    return path.join(process.resourcesPath, 'engine');
  }
  // dev: <desktop>/../engine
  return path.resolve(__dirname, '..', '..', 'engine');
}

/** venv python 실행파일 경로 (플랫폼별) — stdlib venv 레이아웃과 portable 레이아웃 둘 다 확인 */
function resolvePythonExe(engineDir) {
  const override = process.env.SECUREDOC_PYTHON;
  if (override && override.trim()) {
    return path.resolve(override.trim());
  }
  if (process.platform === 'win32') {
    const stdlibVenv = path.join(engineDir, '.venv', 'Scripts', 'python.exe');
    if (safeExists(stdlibVenv)) return stdlibVenv;
    return path.join(engineDir, '.venv', 'python.exe'); // portable(embeddable-style) 레이아웃
  }
  // mac/linux: portable 배포본도 bin/python 레이아웃을 그대로 쓴다(빌드 스크립트가
  // bin/python 심볼릭 링크를 보장) — 별도 fallback 불필요.
  return path.join(engineDir, '.venv', 'bin', 'python');
}

/**
 * 엔진 SQLite store 경로 (PLAN §9.1). REST 로 통계 엔드포인트가 없으므로
 * 앱은 이 파일을 read-only 로 읽어 탐지 카운트를 얻는다(engine 수정 금지 준수).
 * config.py: STORE_DIR = engine/app/store/data, DB = securedoc.sqlite3
 */
function resolveStoreDbPath(engineDir) {
  const override = process.env.SECUREDOC_STORE_DIR;
  const dir = override && override.trim()
    ? path.resolve(override.trim())
    : path.join(engineDir, 'app', 'store', 'data');
  return path.join(dir, 'securedoc.sqlite3');
}

/** 실행 가능 여부 진단 (스폰 전에 명확한 에러 메시지를 주기 위함) */
function diagnose(engineDir, pythonExe) {
  return {
    engineDir,
    pythonExe,
    engineDirExists: safeExists(engineDir),
    pythonExeExists: safeExists(pythonExe),
    appMainExists: safeExists(path.join(engineDir, 'app', 'main.py')),
  };
}

function safeExists(p) {
  try {
    return fs.existsSync(p);
  } catch {
    return false;
  }
}

module.exports = {
  resolveEngineDir,
  resolvePythonExe,
  resolveStoreDbPath,
  diagnose,
};
