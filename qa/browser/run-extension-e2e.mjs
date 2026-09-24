import assert from 'node:assert/strict';
import { cp, mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';
import { chromiumExecutable } from './browser.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, '..', '..');
const sourceExtension = path.join(repoRoot, 'extension');
const resultsDir = path.join(here, 'test-results');

const sites = [
  {
    host: 'chatgpt.com',
    editor: '<div id="prompt-textarea" contenteditable="true"></div>',
    editorSelector: '#prompt-textarea',
    button: '<button id="fixture-submit" type="button" data-testid="send-button">Send</button>',
  },
  {
    host: 'claude.ai',
    editor: '<div data-testid="chat-input" contenteditable="true"></div>',
    editorSelector: '[data-testid="chat-input"]',
    button: '<button id="fixture-submit" type="button" aria-label="Send message">Send</button>',
  },
  {
    host: 'gemini.google.com',
    editor: '<div class="ql-editor" role="textbox" contenteditable="true"></div>',
    editorSelector: '.ql-editor[role="textbox"]',
    button: '<button id="fixture-submit" type="button" aria-label="Send message">Send</button>',
  },
  {
    host: 'copilot.microsoft.com',
    editor: '<textarea data-testid="composer-input"></textarea>',
    editorSelector: '[data-testid="composer-input"]',
    button: '<button id="fixture-submit" type="button" data-testid="submit-button">Send</button>',
  },
  {
    host: 'grok.com',
    editor: '<div aria-label="Ask Grok anything" contenteditable="true"></div>',
    editorSelector: '[aria-label="Ask Grok anything"]',
    button: '<button id="fixture-submit" type="button" data-testid="chat-submit">Send</button>',
  },
  {
    host: 'perplexity.ai',
    editor: '<div id="ask-input" data-lexical-editor="true" role="textbox" contenteditable="true"></div>',
    editorSelector: '#ask-input',
    button: '<button id="fixture-submit" type="button" aria-label="Submit">Send</button>',
  },
];

const requestedConcurrency = Number(process.env.CAMPFIRE_QA_CONCURRENCY || 6);
const concurrency = Number.isInteger(requestedConcurrency)
  ? Math.min(sites.length, Math.max(1, requestedConcurrency)) : 6;

const ORIGINAL_PROMPT = '연락처 010-9876-5432';
const ORIGINAL_FILE_TEXT = 'ORIGINAL_PRIVATE_FILE 010-9876-5432';

function fixtureHtml(site) {
  return `<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <title>Campfire QA - ${site.host}</title>
  <style>
    body { font-family: sans-serif; margin: 24px; }
    #composer { border: 1px solid #aaa; padding: 16px; width: 640px; }
    [contenteditable="true"], textarea { display: block; box-sizing: border-box; width: 600px; min-height: 80px; margin-bottom: 12px; }
    #attachment-chip { min-height: 24px; margin: 8px 0; }
  </style>
</head>
<body>
  <form id="composer" onsubmit="return false">
    ${site.editor}
    <input id="fixture-file" type="file" />
    <div id="attachment-chip"></div>
    ${site.button}
  </form>
  <script>
    (() => {
      const editor = document.querySelector(${JSON.stringify(site.editorSelector)});
      const input = document.querySelector('#fixture-file');
      const button = document.querySelector('#fixture-submit');
      const chip = document.querySelector('#attachment-chip');
      window.__campfireFixture = { attachment: null, submission: null };

      const readFile = async (file) => file ? ({
        name: file.name,
        type: file.type,
        size: file.size,
        text: new TextDecoder().decode(await file.arrayBuffer()),
      }) : null;
      const editorText = () => 'value' in editor ? editor.value : editor.innerText;

      input.addEventListener('change', async () => {
        const observed = await readFile(input.files && input.files[0]);
        window.__campfireFixture.attachment = observed;
        chip.textContent = observed ? observed.name : '';
      });

      button.addEventListener('click', async () => {
        window.__campfireFixture.submission = {
          text: editorText(),
          file: await readFile(input.files && input.files[0]),
        };
      });
    })();
  </script>
</body>
</html>`;
}

async function makeQaExtension(tempRoot) {
  const extensionDir = path.join(tempRoot, 'extension');
  await cp(sourceExtension, extensionDir, { recursive: true });
  await cp(path.join(here, 'test-background.js'), path.join(extensionDir, 'qa-background.js'));
  const manifestPath = path.join(extensionDir, 'manifest.json');
  const manifest = JSON.parse(await readFile(manifestPath, 'utf8'));
  manifest.name = `${manifest.name} QA Harness`;
  manifest.background = { service_worker: 'qa-background.js', type: 'module' };
  await writeFile(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`, 'utf8');
  return extensionDir;
}

async function main() {
  const startedAt = performance.now();
  await mkdir(resultsDir, { recursive: true });
  const tempRoot = await mkdtemp(path.join(os.tmpdir(), 'campfire-browser-qa-'));
  const userDataDir = path.join(tempRoot, 'profile');
  const extensionDir = await makeQaExtension(tempRoot);
  const results = [];
  let context;

  try {
    const executablePath = await chromiumExecutable();
    context = await chromium.launchPersistentContext(userDataDir, {
      ...(executablePath ? { executablePath } : { channel: 'chromium' }),
      headless: true,
      args: [
        `--disable-extensions-except=${extensionDir}`,
        `--load-extension=${extensionDir}`,
      ],
    });

    await context.route('**/*', async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      const site = sites.find((candidate) => candidate.host === url.hostname);
      if (site && request.resourceType() === 'document') {
        await route.fulfill({ status: 200, contentType: 'text/html; charset=utf-8', body: fixtureHtml(site) });
        return;
      }
      await route.abort('blockedbyclient');
    });

    const runSite = async (site) => {
      const siteStartedAt = performance.now();
      const page = await context.newPage();
      const browserErrors = [];
      page.on('pageerror', (error) => browserErrors.push(String(error)));
      page.on('console', (message) => {
        if (message.type() === 'error') browserErrors.push(message.text());
      });

      try {
        await page.goto(`https://${site.host}/campfire-qa`, { waitUntil: 'domcontentloaded' });
        const editor = page.locator(site.editorSelector);
        await editor.click();
        await editor.fill(ORIGINAL_PROMPT);
        await page.locator('#fixture-file').setInputFiles({
          name: 'fixture-original.pdf',
          mimeType: 'application/pdf',
          buffer: Buffer.from(ORIGINAL_FILE_TEXT, 'utf8'),
        });
        await page.locator('#fixture-submit').click();
        await page.waitForFunction(() => window.__campfireFixture?.submission?.file, null, { timeout: 30_000 });

        const submission = await page.evaluate(() => window.__campfireFixture.submission);
        assert.ok(submission.text.includes('[전화번호 마스킹]'), `${site.host}: masked prompt missing`);
        assert.ok(!submission.text.includes('010-9876-5432'), `${site.host}: original prompt leaked`);
        assert.equal(submission.file.name, 'fixture-original_masked.pdf', `${site.host}: masked file name mismatch`);
        assert.ok(submission.file.text.includes('MASKED_FILE_CONTENT'), `${site.host}: masked file content missing`);
        assert.ok(!submission.file.text.includes('ORIGINAL_PRIVATE_FILE'), `${site.host}: original file leaked`);

        results.push({ site: site.host, status: 'passed', durationMs: Math.round(performance.now() - siteStartedAt), submission, browserErrors });
        console.log(`PASS ${site.host}: prompt and file were replaced before submit`);
      } catch (error) {
        const screenshot = path.join(resultsDir, `${site.host.replaceAll('.', '-')}.png`);
        await page.screenshot({ path: screenshot, fullPage: true }).catch(() => {});
        results.push({ site: site.host, status: 'failed', durationMs: Math.round(performance.now() - siteStartedAt), error: String(error), browserErrors, screenshot });
        throw error;
      } finally {
        await page.close();
      }
    };
    for (let i = 0; i < sites.length; i += concurrency) {
      const batch = await Promise.allSettled(sites.slice(i, i + concurrency).map(runSite));
      const failure = batch.find((item) => item.status === 'rejected');
      if (failure) throw failure.reason;
    }
  } finally {
    await writeFile(
      path.join(resultsDir, 'extension-e2e.json'),
      `${JSON.stringify({ generatedAt: new Date().toISOString(), durationMs: Math.round(performance.now() - startedAt), concurrency, results }, null, 2)}\n`,
      'utf8',
    );
    await context?.close().catch(() => {});
    await rm(tempRoot, { recursive: true, force: true });
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
