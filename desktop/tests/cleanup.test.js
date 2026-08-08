'use strict';
/**
 * cleanup.js 테스트 (node --test, 의존성 없음).
 *
 * 이 모듈은 사용자 파일을 **되돌릴 수 없게** 지운다. 그래서 지키려는 것이 두 가지다.
 *
 *  1) 사용자 데이터 루트 밖은 절대 못 지운다. 렌더러가 넘기는 건 항목 id 뿐이고 경로는
 *     cleanup.js 안에서만 만들어지지만, 그 표를 나중에 고치다 실수해도(예: dir 을
 *     '..' 로 잘못 적음) 루트 밖으로 새어나가면 안 된다.
 *  2) 모르는 id 는 조용히 무시한다. 목록에 없는 값이 들어와도 아무 일도 일어나지
 *     않아야 하며, "다 지우기" 같은 부작용은 더더욱 없어야 한다.
 *
 * 그리고 엔진이 붙들고 있는 파일(모델 가중치·SQLite)은 멈춘 뒤 지우고, 원래 돌고
 * 있었을 때만 되살려야 한다 — 사용자가 보호를 꺼둔 상태였다면 삭제를 이유로 켜주면
 * 안 된다.
 */

const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const paths = require('../main/paths');
const cleanup = require('../main/cleanup');

/** userDataRoot 를 임시 폴더로 갈아끼운다 — 진짜 사용자 데이터를 건드리지 않는다.
 *
 *  반드시 async 여야 한다. 동기 함수로 두면 fn 이 async 일 때 본문이 실행되기 전에
 *  finally 가 돌아 임시 폴더를 지우고 userDataRoot 를 되돌려버린다(실제로 그렇게
 *  써서 테스트 넷이 엉뚱하게 실패했다). */
async function withTempRoot(fn) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'campfire-cleanup-'));
  const original = paths.userDataRoot;
  paths.userDataRoot = () => root;
  try {
    return await fn(root);
  } finally {
    paths.userDataRoot = original;
    fs.rmSync(root, { recursive: true, force: true });
  }
}

function seed(root, rel, bytes = 16) {
  const dir = path.join(root, rel);
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, 'x.bin'), Buffer.alloc(bytes, 1));
  return dir;
}

/** 엔진 흉내 — 멈춤/시작 호출 순서를 기록한다. */
function fakeEngine(state = 'running') {
  return {
    calls: [],
    getStatus() { return { state }; },
    async stop() { this.calls.push('stop'); },
    async start() { this.calls.push('start'); },
  };
}
const fakeConfig = (securityEnabled = true) => ({ get: (k) => (k === 'securityEnabled' ? securityEnabled : undefined) });

test('scan 은 실제 용량을 재고, 없는 항목은 present:false 로 준다', async () => {
  await withTempRoot((root) => {
    seed(root, 'logs', 100);
    const { items } = cleanup.scan();
    const logs = items.find((i) => i.id === 'logs');
    const models = items.find((i) => i.id === 'models');
    assert.equal(logs.present, true);
    assert.equal(logs.bytes, 100);
    assert.equal(models.present, false, '없는 폴더를 있다고 하면 "지웠다"는 착각을 준다');
    assert.equal(models.bytes, 0);
  });
});

test('고른 항목만 지우고 나머지는 그대로 둔다', async () => {
  await withTempRoot(async (root) => {
    seed(root, 'logs', 50);
    seed(root, 'models', 70);
    const engine = fakeEngine();

    const res = await cleanup.remove(['logs'], { engineManager: engine, config: fakeConfig() });

    assert.equal(fs.existsSync(path.join(root, 'logs')), false);
    assert.equal(fs.existsSync(path.join(root, 'models')), true, '고르지 않은 항목을 지웠다');
    assert.equal(res.freedBytes, 50);
    assert.deepEqual(engine.calls, [], 'logs 는 엔진을 멈출 이유가 없다');
  });
});

test('모르는 id 는 조용히 무시한다 — 아무것도 안 지운다', async () => {
  await withTempRoot(async (root) => {
    seed(root, 'logs', 10);
    const res = await cleanup.remove(['../../etc', 'everything', ''], {
      engineManager: fakeEngine(), config: fakeConfig(),
    });
    assert.equal(fs.existsSync(path.join(root, 'logs')), true);
    assert.deepEqual(res.removed, []);
    assert.equal(res.freedBytes, 0);
  });
});

test('사용자 데이터 루트 밖을 가리키는 항목은 지우지 않는다', async () => {
  await withTempRoot(async (root) => {
    const outside = fs.mkdtempSync(path.join(os.tmpdir(), 'campfire-outside-'));
    fs.writeFileSync(path.join(outside, 'precious.txt'), 'do not delete');

    // ITEMS 를 고치다 실수한 상황을 그대로 만든다.
    const logs = cleanup.ITEMS.find((i) => i.id === 'logs');
    const originalDir = logs.dir;
    logs.dir = () => outside;
    try {
      const res = await cleanup.remove(['logs'], { engineManager: fakeEngine(), config: fakeConfig() });
      assert.equal(
        fs.existsSync(path.join(outside, 'precious.txt')), true,
        '루트 밖을 지웠다 — 항목 표 실수 하나가 임의 폴더 삭제로 이어진다',
      );
      assert.equal(res.removed[0].ok, false);
    } finally {
      logs.dir = originalDir;
      fs.rmSync(outside, { recursive: true, force: true });
    }
  });
});

test('엔진이 쓰는 항목은 멈춘 뒤 지우고 다시 시작한다', async () => {
  await withTempRoot(async (root) => {
    seed(root, 'models', 30);
    const engine = fakeEngine('running');

    await cleanup.remove(['models'], { engineManager: engine, config: fakeConfig(true) });

    assert.deepEqual(engine.calls, ['stop', 'start']);
    assert.equal(fs.existsSync(path.join(root, 'models')), false);
  });
});

test('보호가 꺼져 있었으면 삭제를 이유로 엔진을 켜지 않는다', async () => {
  await withTempRoot(async (root) => {
    seed(root, 'models', 30);
    const engine = fakeEngine('disabled');

    await cleanup.remove(['models'], { engineManager: engine, config: fakeConfig(false) });

    assert.deepEqual(engine.calls, ['stop'], '사용자가 꺼둔 보호를 우리가 켜면 안 된다');
  });
});

test('formatBytes 는 사람이 읽는 단위로 준다', () => {
  assert.equal(cleanup.formatBytes(0), '0 B');
  assert.equal(cleanup.formatBytes(1024), '1.0 KB');
  assert.equal(cleanup.formatBytes(600 * 1024 * 1024), '600 MB');
});
