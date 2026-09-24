import assert from 'node:assert/strict';
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';
import { chromiumExecutable } from './browser.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const extensionDir = path.resolve(here, '..', '..', 'extension');
const resultsDir = path.join(here, 'test-results');
const temp = await mkdtemp(path.join(os.tmpdir(), 'campfire-engine-qa-'));
const originalPhone = '010-9876-5432';
const testFiles = [1, 2].map(n => ({
  name: `engine-qa-${n}.txt`, mimeType: 'text/plain',
  buffer: Buffer.from(`Campfire QA file ${n}: ${originalPhone}`),
}));
const method = process.argv.includes('--drop') ? 'drop-simulated' : 'picker';
const start = performance.now();
let context;
let report = { generatedAt: new Date().toISOString(), status: 'failed' };

try {
  const executablePath = await chromiumExecutable();
  context = await chromium.launchPersistentContext(path.join(temp, 'profile'), {
    ...(executablePath ? { executablePath } : { channel: 'chromium' }),
    headless: true,
    args: [`--disable-extensions-except=${extensionDir}`, `--load-extension=${extensionDir}`],
  });
  await context.route('https://chatgpt.com/campfire-engine-qa', route => route.fulfill({
    contentType: 'text/html; charset=utf-8',
    body: `<!doctype html><meta charset="utf-8">
      <div id="prompt-textarea" contenteditable="true"></div>
      <input id="files" type="file" multiple>
      <button data-testid="send-button">Send</button>
      <script>
        window.__campfireUploads = [];
        document.querySelector('#files').addEventListener('change', async event => {
          window.__campfireUploads = await Promise.all([...event.target.files].map(async file => ({
            name: file.name, text: await file.text(),
          })));
        });
      </script>`,
  }));
  const page = await context.newPage();
  await page.goto('https://chatgpt.com/campfire-engine-qa');
  const worker = context.serviceWorkers().find(item => item.url().endsWith('/background/service-worker.js'))
    || await context.waitForEvent('serviceworker');
  const tabId = await worker.evaluate(async url =>
    (await chrome.tabs.query({})).find(tab => tab.url === url)?.id,
  page.url());
  assert.ok(tabId, 'extension tab ID missing');

  await page.locator('#prompt-textarea').fill(`Campfire QA prompt: ${originalPhone}`);
  if (method === 'drop-simulated') {
    await page.evaluate(files => {
      const transfer = new DataTransfer();
      for (const file of files) transfer.items.add(new File([file.text], file.name, { type: 'text/plain' }));
      const target = document.querySelector('#prompt-textarea');
      for (const type of ['dragenter', 'dragover', 'drop']) {
        target.dispatchEvent(new DragEvent(type, { bubbles: true, cancelable: true, composed: true, dataTransfer: transfer }));
      }
    }, testFiles.map(file => ({ name: file.name, text: file.buffer.toString('utf8') })));
  } else {
    await page.locator('#files').setInputFiles(testFiles);
  }
  await page.locator('[data-testid="send-button"]').click();

  let sessionId, session;
  for (let i = 0; i < 180; i++) {
    const snapshot = await worker.evaluate(() => chrome.storage.session.get('campfireSessionState'));
    const sessions = snapshot.campfireSessionState?.sessions || {};
    const pair = Object.entries(sessions).find(([, value]) => value.tabId === tabId);
    if (pair) [sessionId, session] = pair;
    if (session?.status === 'ready') break;
    await page.waitForTimeout(250);
  }
  assert.equal(session?.status, 'ready', 'real engine scan did not finish');
  assert.equal(session.docs.length, 2, 'two attachments were not staged');
  assert.ok(session.docs.every(doc => doc.status === 'done'), 'file scan failed');
  assert.equal(session.prompt.status, 'done', 'prompt scan failed');
  const scanDurationMs = Math.round(performance.now() - start);

  const panel = await context.newPage();
  await panel.goto(`chrome-extension://${new URL(worker.url()).host}/sidepanel/sidepanel.html?tabId=${tabId}`);
  const decision = await panel.evaluate(({ sessionId, tabId, docs }) =>
    chrome.runtime.sendMessage({
      type: 'PANEL_MULTI_DECISION', sessionId, tabId,
      decision: { action: 'send', prompt: { action: 'masked', unmaskedKeys: [] },
        files: docs.map(doc => ({ id: doc.id, action: 'masked', unmaskedKeys: [] })) },
    }),
  { sessionId, tabId, docs: session.docs });
  assert.equal(decision?.ok, true, 'approval was rejected');
  await page.waitForFunction(() => window.__campfireUploads?.length === 2
    && window.__campfireUploads.every(file => file.name.includes('_masked')), null, { timeout: 15000 });
  const uploads = await page.evaluate(() => window.__campfireUploads);
  assert.ok(uploads.every(file => !file.text.includes('010-9876-5432')), 'original PII leaked');
  assert.ok(uploads.every(file => file.text.includes('마스킹')), 'masked content missing');

  report = { generatedAt: report.generatedAt, status: 'passed', method, scanDurationMs,
    durationMs: Math.round(performance.now() - start),
    files: uploads.map(file => ({ name: file.name, originalLeaked: file.text.includes(originalPhone) })),
    note: 'Real extension and engine, locally fulfilled ChatGPT-shaped page; no external AI site upload.',
  };
  console.log(`PASS real engine (${method}): 2 masked files injected in ${report.durationMs} ms (scan ${scanDurationMs} ms)`);
} catch (error) {
  report.error = String(error);
  report.durationMs = Math.round(performance.now() - start);
  console.error(error);
  process.exitCode = 1;
} finally {
  await mkdir(resultsDir, { recursive: true });
  await writeFile(path.join(resultsDir, 'engine-e2e.json'), `${JSON.stringify(report, null, 2)}\n`);
  await context?.close().catch(() => {});
  await rm(temp, { recursive: true, force: true });
}
