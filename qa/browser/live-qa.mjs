import { mkdir, writeFile } from 'node:fs/promises';
import { spawn } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';
import { chromiumExecutable } from './browser.mjs';
import { isHardFail, statusMark } from './status-classify.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const extensionDir = path.resolve(here, '..', '..', 'extension');
const profileDir = path.join(here, '.qa-profile');
const resultsDir = path.join(here, 'live-results');
const command = process.argv[2] || 'preflight';
const requestedSites = process.argv.find(arg => arg.startsWith('--sites='))?.slice(8)?.split(',');
const method = process.argv.includes('--drop') ? 'drop-cdp' : 'picker';
const sequential = process.argv.includes('--sequential');
const headed = process.argv.includes('--headed');
const settle = process.argv.includes('--settle');
const fileCount = Number(process.argv.find(arg => arg.startsWith('--file-count='))?.slice(13) || 2);
if (!Number.isInteger(fileCount) || fileCount < 1 || fileCount > 2) throw new Error('--file-count must be 1 or 2');

const sites = [
  { host: 'chatgpt.com', editor: '#prompt-textarea', send: '[data-testid="send-button"]' },
  { host: 'claude.ai', editor: '[data-testid="chat-input"]', send: 'button[aria-label="Send message"], button[aria-label="메시지 보내기"]' },
  { host: 'gemini.google.com', editor: '.ql-editor[role="textbox"]', send: 'button[aria-label="Send message"], button[aria-label="메시지 보내기"]' },
  { host: 'copilot.microsoft.com', editor: '[data-testid="composer-input"], #userInput, textarea', send: '[data-testid="submit-button"], button[aria-label*="submit" i], button[aria-label*="제출"]' },
  { host: 'grok.com', editor: '[aria-label="Ask Grok anything"], textarea[aria-label], textarea[placeholder], [contenteditable="true"]', send: '[data-testid="chat-submit"]' },
  { host: 'perplexity.ai', editor: '#ask-input, [data-lexical-editor="true"]', send: 'button[aria-label="Submit"], button[aria-label="제출"]' },
].filter(site => !requestedSites || requestedSites.includes(site.host));

if (!sites.length) throw new Error('No matching sites. Use --sites=chatgpt.com,claude.ai');

const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const elapsed = start => Math.round(performance.now() - start);

function uploadEndpointOf(rawUrl) {
  const url = new URL(rawUrl);
  if (url.hostname === 'push.clients6.google.com' && url.pathname.startsWith('/upload/')) return `${url.origin}/upload/`;
  if (url.hostname === 'claude.ai' && url.pathname.endsWith('/wiggle/upload-file')) return `${url.origin}/wiggle/upload-file`;
  if (url.hostname === 'copilot.microsoft.com' && url.pathname === '/c/api/attachments') return `${url.origin}/c/api/attachments`;
  if (url.hostname === 'grok.com' && url.pathname === '/http/upload-file-v2/direct') return `${url.origin}/http/upload-file-v2/direct`;
  if (url.hostname === 'www.perplexity.ai' && url.pathname === '/rest/uploads/batch_create_upload_urls') {
    return `${url.origin}/rest/uploads/batch_create_upload_urls`;
  }
  if (/\.amazonaws\.com$|\.storage\.googleapis\.com$/.test(url.hostname)) return `${url.origin}/object-upload`;
  return null;
}

async function launch(headless) {
  await mkdir(profileDir, { recursive: true });
  const browserPath = await chromiumExecutable();
  return chromium.launchPersistentContext(profileDir, {
    ...(browserPath ? { executablePath: browserPath } : { channel: 'chromium' }),
    headless,
    viewport: { width: 1400, height: 900 },
    args: [
      `--disable-extensions-except=${extensionDir}`,
      `--load-extension=${extensionDir}`,
      ...(headed ? ['--start-minimized'] : []),
    ],
  });
}

async function manualBrowser(withExtension) {
  await mkdir(profileDir, { recursive: true });
  const browserPath = await chromiumExecutable() || chromium.executablePath();
  console.log(`QA profile: ${profileDir}`);
  console.log(withExtension
    ? 'Opening manual QA browser with Campfire, without Playwright control. Close it when finished.'
    : 'Opening the QA browser without Playwright control. Close all QA browser windows when sign-in is complete.');
  const browser = spawn(browserPath, [
    `--user-data-dir=${profileDir}`,
    '--no-first-run',
    '--no-default-browser-check',
    ...(withExtension ? [`--disable-extensions-except=${extensionDir}`, `--load-extension=${extensionDir}`] : []),
    '--new-window',
    ...sites.map(site => `https://${site.host}/`),
  ], { stdio: 'inherit' });
  await new Promise((resolve, reject) => {
    browser.once('error', reject);
    browser.once('exit', (code, signal) => code === 0 ? resolve() : reject(new Error(`QA browser exited: ${code ?? signal}`)));
  });
}

async function workerOf(context) {
  return context.serviceWorkers().find(worker => worker.url().endsWith('/background/service-worker.js'))
    || context.waitForEvent('serviceworker', { timeout: 15000 });
}

async function engineHealth() {
  for (let port = 48200; port <= 48209; port++) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/health`, { signal: AbortSignal.timeout(700) });
      const data = await response.json();
      if (response.ok && data.service === 'campfire') return { ok: true, port };
    } catch { /* next port */ }
  }
  return { ok: false, reason: 'Campfire engine is not running on ports 48200-48209' };
}

async function tabIdFor(worker, url) {
  return worker.evaluate(async url => {
    const tabs = await chrome.tabs.query({});
    return tabs.find(tab => tab.url === url)?.id ?? null;
  }, url);
}

async function sessionFor(worker, tabId) {
  return worker.evaluate(async tabId => {
    const state = (await chrome.storage.session.get('campfireSessionState')).campfireSessionState;
    const pair = Object.entries(state?.sessions || {}).find(([, session]) => session.tabId === tabId);
    return pair ? { id: pair[0], ...pair[1] } : null;
  }, tabId);
}

async function waitForSession(worker, tabId, timeoutMs = 90000) {
  const start = performance.now();
  while (elapsed(start) < timeoutMs) {
    const session = await sessionFor(worker, tabId);
    if (session?.status === 'ready' || session?.status === 'error' || session?.status === 'cancelled') return session;
    await pause(250);
  }
  return sessionFor(worker, tabId);
}

async function findEditor(page, site) {
  await page.waitForTimeout(900);
  const editor = page.locator(site.editor).first();
  let lastBlocker = null;
  for (let i = 0; i < 120; i++) {
    const blocker = await blockerOf(page);
    if (blocker === 'region-blocked') throw new Error(blocker);
    if (blocker) {
      lastBlocker = blocker;
      await page.waitForTimeout(250);
      continue;
    }
    if (await editor.isVisible().catch(() => false)) {
      await page.waitForTimeout(600);
      const stableBlocker = await blockerOf(page);
      if (stableBlocker) { lastBlocker = stableBlocker; continue; }
      if (await editor.isVisible().catch(() => false)) return editor;
    }
    await page.waitForTimeout(250);
  }
  throw new Error(lastBlocker || 'Editor not visible after 30 seconds');
}

async function blockerOf(page) {
  return page.locator('body').evaluate(body => {
    const text = body.innerText;
    if (/잠시만 기다리십시오|just a moment/i.test(document.title)) return 'human-verification-needed';
    if (/해당 지역에서 사용할 수 없음|not available in your region|not available in your country/i.test(text)) return 'region-blocked';
    if (/사람인지 확인하십시오|보안 확인 수행 중|verify you are human|checking your browser/i.test(text)
        || document.querySelector('iframe[src*="challenges.cloudflare.com"]')) return 'human-verification-needed';
    return null;
  }).catch(() => null);
}

function hasProviderUploadLimit(site, bodyText) {
  if (site.host !== 'chatgpt.com') return false;
  return /파일 업로드(?:를)? 모두 사용|파일 업로드 남은 횟수:\s*0회|file uploads? (?:have been )?(?:all )?used|upload limit/i
    .test(bodyText || '');
}

async function ensureFileInput(page, site) {
  let inputCount = await page.locator('input[type="file"]').count();
  if (!inputCount && site.host === 'gemini.google.com') {
    const tools = page.getByRole('button', { name: /업로드 및 도구|Upload and tools/i });
    if (await tools.count()) {
      await tools.first().click();
      await page.waitForTimeout(300);
      inputCount = await page.locator('input[type="file"]').count();
    }
  }
  return inputCount;
}

async function inspectSite(context, site) {
  const start = performance.now();
  const page = await context.newPage();
  try {
    await page.goto(`https://${site.host}/`, { waitUntil: 'domcontentloaded', timeout: 30000 });
    await findEditor(page, site);
    const inputCount = await ensureFileInput(page, site);
    const fileInputDetails = await page.locator('input[type="file"]').evaluateAll(inputs => inputs.map(input => ({
      accept: input.accept,
      multiple: input.multiple,
      connected: input.isConnected,
    })));
    const uploadControls = await page.locator('button, [role="menuitem"]').evaluateAll(buttons =>
      buttons.map(button => button.getAttribute('aria-label') || button.getAttribute('title') || button.innerText)
        .map(label => label?.trim()).filter(label => label === '+' || /attach|upload|add|file|첨부|업로드|추가|파일/i.test(label || ''))
        .slice(0, 12));
    let attachmentMenu = [];
    let draftButtons = [];
    let claudeDraftState = null;
    if (site.host === 'claude.ai') {
      claudeDraftState = await page.evaluate(() => ({
        loadingAttachment: document.body.innerText.includes('첨부 파일 로드 중')
          || document.body.innerText.includes('Loading attachment'),
        newChatControls: [...document.querySelectorAll('a, button')]
          .filter(element => /새로 생성|New chat/i.test(element.textContent || element.getAttribute('aria-label') || ''))
          .slice(0, 5).map(element => ({ tag: element.tagName, role: element.getAttribute('role'),
            ariaLabel: element.getAttribute('aria-label'), href: element.getAttribute('href') })),
      }));
      draftButtons = await page.locator('button[data-cds-attachment-remove][aria-label^="CFQA-"]')
        .evaluateAll(buttons => buttons.map(button => button.getAttribute('aria-label')));
      const button = page.getByRole('button', { name: /파일, 커넥터 등 추가|Add files and more/i });
      if (await button.count()) {
        await button.first().click();
        attachmentMenu = await page.getByRole('menuitem').allTextContents();
      }
    }
    return { site: site.host, status: inputCount ? 'ready' : 'upload-control-needed',
      url: page.url(), fileInputs: inputCount, fileInputDetails, uploadControls, attachmentMenu, draftButtons, claudeDraftState, durationMs: elapsed(start) };
  } catch (error) {
    const blocker = await blockerOf(page);
    const diagnostics = await page.evaluate(() => ({
      title: document.title,
      editorCandidates: [...document.querySelectorAll('textarea, [contenteditable="true"], [role="textbox"]')]
        .slice(0, 8).map(element => ({
          tag: element.tagName.toLowerCase(), role: element.getAttribute('role'),
          ariaLabel: element.getAttribute('aria-label'), placeholder: element.getAttribute('placeholder'),
          editable: element.getAttribute('contenteditable'),
        })),
    })).catch(() => ({}));
    return { site: site.host, status: blocker || 'login-or-selector-needed',
      url: page.url(), ...diagnostics, error: String(error), durationMs: elapsed(start) };
  } finally {
    await page.close();
  }
}

async function attachFiles(page, site, files) {
  if (method === 'drop-cdp') {
    await ensureFileInput(page, site);
    const cdp = await page.context().newCDPSession(page);
    const target = page.locator('main').first();
    const box = await target.boundingBox() || await page.locator('body').boundingBox();
    if (!box) throw new Error('Drop target has no visible bounds');
    const x = box.x + box.width / 2;
    const y = box.y + box.height / 2;
    for (const selected of sequential ? files.map(file => [file]) : [files]) {
      const paths = [];
      for (const file of selected) {
        const filePath = path.join(resultsDir, file.name);
        await writeFile(filePath, file.text);
        paths.push(filePath);
      }
      const data = { items: [], files: paths, dragOperationsMask: 1 };
      await cdp.send('Input.dispatchDragEvent', { type: 'dragEnter', x, y, data });
      await cdp.send('Input.dispatchDragEvent', { type: 'dragOver', x, y, data });
      await cdp.send('Input.dispatchDragEvent', { type: 'drop', x, y, data });
      if (sequential) await pause(750);
    }
    await cdp.detach();
  } else {
    await ensureFileInput(page, site);
    const buffers = files.map(file => ({ name: file.name, mimeType: 'text/plain', buffer: Buffer.from(file.text) }));
    if (site.host === 'claude.ai') {
      for (const selected of sequential ? buffers.map(buffer => [buffer]) : [buffers]) {
        await page.getByRole('button', { name: /파일, 커넥터 등 추가|Add files and more/i }).click();
        const chooserPromise = page.waitForEvent('filechooser', { timeout: 10000 });
        await page.getByRole('menuitem', { name: /파일 또는 사진 추가|Add files or photos/i }).click();
        await (await chooserPromise).setFiles(selected);
      }
    } else {
      const input = page.locator('input[type="file"]').first();
      await input.waitFor({ state: 'attached', timeout: 10000 });
      if (await input.getAttribute('multiple') !== null) {
        await input.setInputFiles(buffers);
      } else {
        for (const buffer of buffers) await page.locator('input[type="file"]').first().setInputFiles(buffer);
      }
    }
  }
  await page.locator('#__ups_pending_badge').waitFor({ state: 'attached', timeout: 10000 });
}

async function clearOwnQaDraft(page, site) {
  if (site.host !== 'claude.ai') return 0;
  const removeButtons = page.locator('button[data-cds-attachment-remove]');
  // Inspect the labels directly: do not remove anything outside our synthetic QA files.
  const labels = await removeButtons.evaluateAll(buttons => buttons.map(button => button.getAttribute('aria-label') || ''));
  const isQaAttachment = label => label.startsWith('CFQA-') || label.startsWith('CLAUDEPROBE-');
  if (labels.some(label => !isQaAttachment(label))) throw new Error('Unrelated Claude draft attachment is present');
  let removed = 0;
  while (await removeButtons.count()) {
    if (removed >= 30) throw new Error('Too many QA draft attachments to clear safely');
    await removeButtons.first().click();
    removed += 1;
  }
  return removed;
}

async function sendDecision(context, worker, tabId, session, action) {
  const extensionId = new URL(worker.url()).host;
  const panel = await context.newPage();
  try {
    await panel.goto(`chrome-extension://${extensionId}/sidepanel/sidepanel.html?tabId=${tabId}`);
    const promptFindingCount = (session.prompt?.counts?.pii || 0)
      + (session.prompt?.counts?.injection || 0);
    const decision = action === 'cancel' ? { action: 'cancel' } : {
      // QA prompts contain only synthetic instructions. Keep every detected segment
      // so a prompt false positive cannot hide the request used for content proof.
      action: 'send', prompt: {
        action: 'masked',
        unmaskedKeys: Array.from({ length: promptFindingCount }, (_, i) => `prompt:${i}`),
      },
      files: session.docs.map(doc => ({ id: doc.id, action: 'masked', unmaskedKeys: [] })),
    };
    return await panel.evaluate(({ sessionId, tabId, decision }) =>
      chrome.runtime.sendMessage({ type: 'PANEL_MULTI_DECISION', sessionId, tabId, decision }),
    { sessionId: session.id, tabId, decision });
  } finally {
    await panel.close().catch(() => {});
  }
}

async function runSite(context, worker, site) {
  const start = performance.now();
  const page = await context.newPage();
  const marker = `CFQA-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
  const files = Array.from({ length: fileCount }, (_, index) => {
    const n = index + 1;
    const key = `${n === 1 ? 'ALPHA' : 'BRAVO'}-${marker.slice(5, 18)}`;
    return { name: `${marker}-${n}.txt`, key, text: `QA_KEY: ${key}\n010-9876-5432` };
  });
  const requests = { masked: false, original: false, responses: [] };
  const outbound = [];
  const diagnostics = [];
  page.on('console', message => {
    const value = message.text();
    if (value.startsWith('[SecureDoc]') && diagnostics.length < 100) diagnostics.push(value.slice(0, 500));
  });
  page.on('pageerror', error => {
    if (diagnostics.length < 100) diagnostics.push(`Page error: ${String(error).slice(0, 300)}`);
  });
  const maskedRequests = new WeakMap();
  const maskedBase = file => file.name.replace('.txt', '_masked');
  const isUploadReceipt = response => !/\/batch_create_upload_urls(?:$|\/)/.test(response.endpoint);
  const actualUploadResponses = () => requests.responses.filter(isUploadReceipt);
  const allUploadsOk = () => files.every(file => actualUploadResponses().some(response =>
    response.files.includes(maskedBase(file)) && response.status >= 200 && response.status < 300));
  page.on('request', request => {
    if (!['POST', 'PUT', 'PATCH'].includes(request.method())) return;
    const body = request.postData() || '';
    const matched = files.map(maskedBase).filter(name => body.includes(name));
    if (matched.length) {
      requests.masked = true;
      maskedRequests.set(request, matched);
    }
    if (files.some(file => body.includes(file.name)) || body.includes('010-9876-5432')) requests.original = true;
  });
  page.on('response', response => {
    const endpoint = uploadEndpointOf(response.url());
    if (endpoint && ['POST', 'PUT', 'PATCH'].includes(response.request().method()) && outbound.length < 100) {
      outbound.push({ method: response.request().method(), status: response.status(), endpoint });
    }
    const matched = maskedRequests.get(response.request());
    if (matched && endpoint) {
      requests.responses.push({ status: response.status(), endpoint, files: matched });
    }
  });
  let tabId = null;
  let session = null;
  let approved = false;
  let phase = 'navigation';
  try {
    await page.goto(`https://${site.host}/`, { waitUntil: 'domcontentloaded', timeout: 30000 });
    phase = 'editor';
    const editor = await findEditor(page, site);
    const clearedQaDrafts = await clearOwnQaDraft(page, site);
    if (clearedQaDrafts) diagnostics.push(`Cleared ${clearedQaDrafts} stale CFQA attachments`);
    tabId = await tabIdFor(worker, page.url());
    if (tabId == null) throw new Error('Chrome tab ID not found');
    await editor.fill(`${marker}: 첨부한 문서의 QA_KEY 값을 각각 알려줘.`);
    phase = 'attachment';
    await attachFiles(page, site, files);
    phase = 'submit';
    await page.locator(site.send).first().click({ timeout: 10000 });
    phase = 'scan';
    session = await waitForSession(worker, tabId);
    if (!session || session.status !== 'ready') throw new Error(`Scan not ready: ${session?.status || 'missing'}`);
    if (session.docs?.length !== fileCount || session.docs.some(doc => doc.status !== 'done' || !doc.counts?.pii)
        || session.prompt?.status !== 'done') {
      throw new Error(`Scan incomplete: ${session.docs?.map(doc => doc.status).join(',')} / prompt=${session.prompt?.status}`);
    }
    phase = 'approval';
    const response = await sendDecision(context, worker, tabId, session, 'send');
    if (!response?.ok) throw new Error(`Approval failed: ${JSON.stringify(response)}`);
    approved = true;
    const evidenceStart = performance.now();
    while (elapsed(evidenceStart) < (settle ? 90000 : 12000)) {
      if (requests.original) throw new Error('Original filename appeared in outbound request');
      if (diagnostics.some(value => value.includes('문서를 첨부하지 못해 전송을 중단했습니다'))) break;
      const body = await page.locator('body').innerText().catch(() => '');
      if (hasProviderUploadLimit(site, body)) break;
      const contentReadOk = files.every(file => body.includes(file.key));
      const contentResponseOk = contentReadOk && files.every(file => body.includes(maskedBase(file)));
      if (contentResponseOk || (allUploadsOk() && (!settle || contentReadOk)) || (!settle && files.every(file => body.includes(maskedBase(file))))
          || (!settle && requests.masked && elapsed(evidenceStart) > 5000)) break;
      await pause(500);
    }
    const body = await page.locator('body').innerText().catch(() => '');
    const visibleMasked = files.map(file => body.includes(maskedBase(file)));
    const uiEvidence = {
      markerPresent: body.includes(marker.slice(0, 14)),
      maskedNames: files.filter(file => body.includes(maskedBase(file))).map(file => maskedBase(file)),
      qaOkVisible: body.includes('QA OK'),
      responseKeys: files.filter(file => body.includes(file.key)).map(file => file.key),
      matchingLabels: await page.locator('[title], [aria-label]').evaluateAll((elements, prefix) =>
        elements.flatMap(element => [element.getAttribute('title'), element.getAttribute('aria-label')])
          .filter(value => value?.includes(prefix)).slice(0, 20), marker.slice(0, 14)),
    };
    const uploadOk = allUploadsOk();
    const contentReadOk = files.every(file => body.includes(file.key));
    const siteUploadOk = ['copilot.microsoft.com', 'grok.com'].includes(site.host)
      && visibleMasked.every(Boolean)
      && outbound.filter(response => response.status >= 200 && response.status < 300
        && !response.endpoint.endsWith('/object-upload')).length >= files.length;
    const finalSession = await sessionFor(worker, tabId);
    const fileInputs = await page.locator('input[type="file"]').evaluateAll(inputs =>
      inputs.map(input => [...input.files].map(file => file.name)));
    const namesInInputs = fileInputs.flat();
    const maskedInputOnly = files.every(file => namesInInputs.some(name => name.startsWith(maskedBase(file))));
    const transportOk = uploadOk || siteUploadOk;
    const contentResponseOk = contentReadOk && visibleMasked.every(Boolean);
    const providerUploadLimit = hasProviderUploadLimit(site, body);
    const attachFailed = diagnostics.some(value => value.includes('문서를 첨부하지 못해 전송을 중단했습니다'));
    const status = requests.original ? 'failed-leak' : providerUploadLimit ? 'provider-upload-limit'
      : attachFailed ? 'attachment-reinject-failed'
      : contentResponseOk ? 'content-response-ok'
      : transportOk && (!settle || contentReadOk) ? 'upload-response-ok'
      : transportOk ? 'uploaded-awaiting-content-proof'
      : actualUploadResponses().length ? 'partial-upload-response' : requests.masked ? 'upload-request-observed'
        : visibleMasked.every(Boolean) ? 'masked-in-composer'
          : maskedInputOnly ? 'masked-input-only' : 'inconclusive';
    const screenshot = ['upload-response-ok', 'content-response-ok'].includes(status) ? null
      : path.join(resultsDir, `${site.host.replaceAll('.', '-')}.png`);
    if (screenshot) await page.screenshot({ path: screenshot, fullPage: true }).catch(() => {});
    return {
      site: site.host, status,
      method, durationMs: elapsed(start), docs: session.docs.map(doc => doc.status),
      maskedRequest: requests.masked, uploadResponses: requests.responses, maskedVisible: visibleMasked,
      providerUploadLimit, finalSessionStatus: finalSession?.status || null,
      fileInputs, screenshot, diagnostics, outbound, uiEvidence,
      note: 'Approval used the real extension service worker; native side-panel UI was not clicked.',
    };
  } catch (error) {
    if (!approved && tabId != null) {
      session ||= await sessionFor(worker, tabId).catch(() => null);
      if (session?.id) await sendDecision(context, worker, tabId, session, 'cancel').catch(() => {});
    }
    const screenshot = path.join(resultsDir, `${site.host.replaceAll('.', '-')}.png`);
    await page.screenshot({ path: screenshot, fullPage: true }).catch(() => {});
    const blocker = await blockerOf(page);
    const status = blocker || (phase === 'editor' ? 'login-or-selector-needed'
      : phase === 'attachment' ? 'upload-control-needed' : 'failed');
    return { site: site.host, status, phase,
      method, error: String(error), durationMs: elapsed(start), screenshot, diagnostics, outbound };
  } finally {
    await page.close();
  }
}

async function main() {
  if (!['init', 'login', 'manual', 'preflight', 'run'].includes(command)) throw new Error('Use init, login, manual, preflight, or run');
  if (command === 'login' || command === 'manual') return manualBrowser(command === 'manual');
  await mkdir(resultsDir, { recursive: true });
  const started = performance.now();
  const context = await launch(!headed);
  try {
    const worker = await workerOf(context);
    console.log(`QA profile: ${profileDir}`);
    console.log(`Extension: ${new URL(worker.url()).host}`);
    if (command === 'init') return;
    const health = await engineHealth();
    const results = command === 'preflight'
      ? await Promise.all(sites.map(site => inspectSite(context, site)))
      : health.ok ? await Promise.all(sites.map(async site => runSite(context, worker, site))) : [];
    const report = { generatedAt: new Date().toISOString(), command, headless: !headed,
      profileDir, engine: health, durationMs: elapsed(started), results };
    const siteSuffix = sites.map(site => site.host.split('.')[0]).join('+');
    const reportSuffix = command === 'run'
      ? `-${method}${sequential ? '-sequential' : ''}-${siteSuffix}` : '';
    const reportPath = path.join(resultsDir, `${command}${reportSuffix}.json`);
    await writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`);
    console.log(JSON.stringify({ durationMs: report.durationMs, engine: health,
      results: results.map(result => ({ site: result.site, status: result.status, mark: statusMark(command, result.status), durationMs: result.durationMs })),
      reportPath }, null, 2));
    const hardFails = results.filter(result => isHardFail(command, result.status));
    const blocked = results.filter(result => statusMark(command, result.status) === 'blocked(manual)');
    if (blocked.length) console.log(`WARN 자동 검증 불가(수동 확인 대상): ${blocked.map(result => `${result.site}=${result.status}`).join(', ')}`);
    if (hardFails.length) console.error(`FAIL: ${hardFails.map(result => `${result.site}=${result.status}`).join(', ')}`);
    if (!health.ok || hardFails.length) process.exitCode = 1;
  } finally {
    await context.close();
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
