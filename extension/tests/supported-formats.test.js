'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..', '..');

function engineExtensions() {
  const config = fs.readFileSync(path.join(root, 'engine', 'app', 'config.py'), 'utf8');
  const block = config.match(/SUPPORTED_EXTENSIONS:\s*set\[str\]\s*=\s*\{([\s\S]*?)\n\}/);
  assert.ok(block, 'engine SUPPORTED_EXTENSIONS block not found');
  return [...block[1].matchAll(/["'](\.[a-z0-9]+)["']/gi)].map(match => match[1]);
}

function extensionRegex(relativePath) {
  const source = fs.readFileSync(path.join(root, 'extension', relativePath), 'utf8');
  const literal = source.match(/const SUPPORTED_EXTS\s*=\s*(\/\\\.\([^)]+\)\$\/i)/);
  assert.ok(literal, `${relativePath} SUPPORTED_EXTS not found`);
  return vm.runInNewContext(literal[1]);
}

for (const file of ['content/content.js', 'content/interceptor.js']) {
  test(`${file} intercepts every engine-supported extension`, () => {
    const supported = extensionRegex(file);
    for (const extension of engineExtensions()) {
      assert.match(`sample${extension}`, supported, `${extension} is missing from ${file}`);
    }
    assert.match('pii.py', supported);
  });
}
