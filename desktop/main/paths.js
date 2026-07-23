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
 *   - 명령 = <engineDir>/.venv/Scripts/python.exe -m app.main   (win)
 *            <engineDir>/.venv/bin/python       -m app.main   (mac/linux)
 *   - 엔진은 스스로 48200~48209 포트를 스캔해 바인딩하고 /health 로 실제 포트를 알린다(PLAN §11).
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

/** venv python 실행파일 경로 (플랫폼별) */
function resolvePythonExe(engineDir) {
  const override = process.env.SECUREDOC_PYTHON;
  if (override && override.trim()) {
    return path.resolve(override.trim());
  }
  if (process.platform === 'win32') {
    return path.join(engineDir, '.venv', 'Scripts', 'python.exe');
  }
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
