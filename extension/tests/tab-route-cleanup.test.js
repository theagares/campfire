'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(
  path.join(__dirname, '..', 'background', 'service-worker.js'),
  'utf8',
);

test('closed tabs delete their engine route report', async () => {
  const fn = source.match(/async function removeProxyRoute\(tabId\) \{[\s\S]*?\n\}/);
  assert.ok(fn, 'removeProxyRoute function not found');
  assert.match(source, /chrome\.tabs\.onRemoved\?\.addListener\(\(tabId\)/);

  const calls = [];
  const context = vm.createContext({
    fetchServer: async (url, options) => {
      calls.push({ url, options });
      return { res: { ok: true } };
    },
  });
  vm.runInContext(`${fn[0]}; globalThis.removeProxyRoute = removeProxyRoute;`, context);

  assert.equal(await context.removeProxyRoute(27), true);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/proxy/extension-route/27');
  assert.equal(calls[0].options.method, 'DELETE');
  assert.equal(calls[0].options.cache, 'no-store');
});
