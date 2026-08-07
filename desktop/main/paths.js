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
const os = require('os');

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

/**
 * @returns {string} Chrome 확장 프로그램(extension/) 루트 디렉터리.
 * prod 는 extraResources 로 번들된 사본(resourcesPath/extension), dev 는 소스트리 형제 폴더.
 * "저장소를 따로 clone/다운로드해야 확장을 로드할 수 있다"는 배포 갭을 없애기 위해
 * 설치 파일 안에 그대로 함께 담아, 설치만 하면 로컬 디스크에 실제 파일이 존재하게 한다.
 */
function resolveExtensionDir(app) {
  const override = process.env.SECUREDOC_EXTENSION_DIR;
  if (override && override.trim()) {
    return path.resolve(override.trim());
  }
  if (app && app.isPackaged) {
    return path.join(process.resourcesPath, 'extension');
  }
  return path.resolve(__dirname, '..', '..', 'extension');
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
 * 사용자 데이터 루트 — **앱 번들 밖**. 엔진 config.py 의 _models_base() 와 같은 규칙을
 * 쓴다(win: %LOCALAPPDATA%, mac: ~/Library/Application Support, linux: XDG).
 *
 * Electron 의 app.getPath('userData') 를 쓰지 않는 이유: Windows 에서 그건 Roaming
 * (%APPDATA%)인데 엔진의 모델 보관 위치는 %LOCALAPPDATA% 라 서로 갈린다. 한 앱의
 * 데이터가 두 군데로 흩어지지 않게, 엔진이 이미 쓰는 규칙에 맞춘다.
 */
function userDataRoot() {
  if (process.platform === 'win32') {
    const base = process.env.LOCALAPPDATA
      || path.join(os.homedir(), 'AppData', 'Local');
    return path.join(base, 'Campfire');
  }
  if (process.platform === 'darwin') {
    return path.join(os.homedir(), 'Library', 'Application Support', 'Campfire');
  }
  const base = process.env.XDG_DATA_HOME || path.join(os.homedir(), '.local', 'share');
  return path.join(base, 'Campfire');
}

/**
 * 엔진 SQLite store 디렉터리 (PLAN §9.1).
 *
 * 예전엔 engineDir/app/store/data, 즉 **앱 번들 내부**였다. macOS 에서 이게 실제로
 * 앱을 못 쓰게 만들었다(실사용자 확인): 번들 안에 securedoc.sqlite3 가 생기면서
 * codesign --verify 가 "file added" 로 실패했다 — ad-hoc 서명이 첫 실행에 스스로
 * 깨진 것이다. 그래서 사용자 데이터 폴더로 옮긴다.
 *
 * 이 값은 앱이 정하고 SECUREDOC_STORE_DIR 로 엔진에 넘긴다(engine-manager). 앱이
 * 읽는 경로와 엔진이 쓰는 경로가 어긋나면 통계가 통째로 빈 채로 보이므로, 결정은
 * 반드시 한 곳에서만 한다.
 */
function resolveStoreDir() {
  const override = process.env.SECUREDOC_STORE_DIR;
  if (override && override.trim()) return path.resolve(override.trim());
  return path.join(userDataRoot(), 'store');
}

function resolveStoreDbPath() {
  return path.join(resolveStoreDir(), 'securedoc.sqlite3');
}

/**
 * Python 바이트코드 캐시(.pyc) 위치 — 반드시 번들 밖이어야 한다.
 *
 * 빌드가 __pycache__ 를 전부 빼고 패키징하므로, 첫 실행 때 Python 이 stdlib 전체를
 * 컴파일하며 번들 안에 .pyc 를 쓴다. 실사용자 mac 에서 codesign --verify 가 바로 그
 * .pyc 들을 "file added" 로 나열하며 실패했다 — 서명이 깨지는 직접 원인이다.
 * PYTHONPYCACHEPREFIX(3.8+)로 캐시를 여기로 돌리면 컴파일 캐시의 속도 이점은 그대로
 * 두면서 번들은 손대지 않는다.
 */
function resolvePycacheDir() {
  return path.join(userDataRoot(), 'pycache');
}

/** 엔진 로그 파일 — 배포본에서 사이드카가 죽었을 때 흔적을 남길 유일한 곳. */
function resolveEngineLogPath() {
  return path.join(userDataRoot(), 'logs', 'engine.log');
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
  resolveExtensionDir,
  resolvePythonExe,
  userDataRoot,
  resolveStoreDir,
  resolveStoreDbPath,
  resolvePycacheDir,
  resolveEngineLogPath,
  diagnose,
};
