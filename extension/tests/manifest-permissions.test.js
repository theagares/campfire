/**
 * Keep the Chrome Web Store permission surface aligned with APIs that Campfire
 * actually uses. Static content_scripts handle site injection, so scripting and
 * activeTab must not be added as speculative permissions.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const extensionRoot = path.join(__dirname, '..');
const manifest = JSON.parse(fs.readFileSync(path.join(extensionRoot, 'manifest.json'), 'utf8'));
const permissions = [...(manifest.permissions || [])].sort();

assert.deepEqual(
  permissions,
  ['sidePanel', 'storage'],
  'manifest permissions changed; verify every requested API permission is actively required',
);

const runtimeSource = [
  path.join(extensionRoot, 'background', 'service-worker.js'),
  path.join(extensionRoot, 'background', 'config.js'),
  path.join(extensionRoot, 'content', 'content.js'),
  path.join(extensionRoot, 'popup', 'popup.js'),
  path.join(extensionRoot, 'sidepanel', 'sidepanel.js'),
].map(file => fs.readFileSync(file, 'utf8')).join('\n');

assert.match(runtimeSource, /chrome\.sidePanel\b/, 'sidePanel permission has no matching API use');
assert.match(runtimeSource, /chrome\.storage\b/, 'storage permission has no matching API use');
assert.doesNotMatch(runtimeSource, /chrome\.scripting\b/, 'scripting API use requires a fresh permission review');

console.log('manifest permissions ok');
