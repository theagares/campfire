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
  ['declarativeNetRequest', 'sidePanel', 'storage'],
  'manifest permissions changed; verify every requested API permission is actively required',
);

// declarativeNetRequest: 입력하는 중에 친 글을 그대로 싣는 자동완성·미리보내기 요청을 막는다
// (2026-10-22 실측 — 전송 버튼을 누르기 전에 나가서 프롬프트 게이트로는 못 막는다. Copilot 은
// 작업자(worker)에서 보내 페이지 fetch 훅도 닿지 않는다). 프록시 addon.py PROMPT_ECHO_* 와 같은 목록.
const rules = JSON.parse(fs.readFileSync(path.join(extensionRoot, 'rules', 'typing-leaks.json'), 'utf8'));
assert.deepEqual(manifest.declarative_net_request?.rule_resources?.map(r => r.path), ['rules/typing-leaks.json']);
for (const filter of [
  '||substrate.office.com/search/api/v1/suggestions',  // Copilot 자동완성
  '||chatgpt.com/backend-api/f/conversation/prepare',  // ChatGPT 미리보내기(partial_query)
  '||grok.com/_worker/typeahead',                     // Grok 자동완성
  '||suggest.perplexity.ai/',                         // Perplexity 자동완성 웹소켓·navigate
  '||perplexity.ai/rest/event/analytics',             // Perplexity 분석(queryStr)
]) {
  const rule = rules.find(r => r.condition.urlFilter === filter);
  assert.ok(rule && rule.action.type === 'block', `${filter} 차단 규칙이 없다`);
}
assert.equal(new Set(rules.map(r => r.id)).size, rules.length, '규칙 id 가 겹친다');
// Copilot 규칙은 copilot.com 이 보낸 것만 — substrate.office.com 은 Outlook 등 M365 가 같이 쓴다.
assert.deepEqual(rules.find(r => r.condition.urlFilter.includes('substrate')).condition.initiatorDomains, ['copilot.com']);

const runtimeSource = [
  path.join(extensionRoot, 'background', 'service-worker.js'),
  path.join(extensionRoot, 'background', 'config.js'),
  path.join(extensionRoot, 'content', 'content.js'),
  path.join(extensionRoot, 'popup', 'popup.js'),
  path.join(extensionRoot, 'sidepanel', 'sidepanel.js'),
].map(file => fs.readFileSync(file, 'utf8')).join('\n');

assert.match(runtimeSource, /chrome\.sidePanel\b/, 'sidePanel permission has no matching API use');
assert.match(runtimeSource, /chrome\.storage\b/, 'storage permission has no matching API use');
assert.match(runtimeSource, /chrome\.declarativeNetRequest\b/, '보호 토글이 차단 규칙을 같이 켜고 끄지 않는다');
assert.doesNotMatch(runtimeSource, /chrome\.scripting\b/, 'scripting API use requires a fresh permission review');

console.log('manifest permissions ok');
