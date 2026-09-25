# Browser QA

This suite loads the real Campfire content scripts into Playwright Chromium and
serves deterministic fixture pages at the six supported production origins.
Playwright fulfils those document requests locally, so no AI account, external
page, or prompt upload is involved.

The QA-only service worker automatically returns a masked prompt and masked file.
It is copied into a temporary extension directory at runtime and is never shipped
with Campfire. The test proves that each site's selectors intercept submission,
replace the prompt and attachment, and prevent the original values from reaching
the fixture site's submit handler. The six fixtures run concurrently by default;
set `CAMPFIRE_QA_CONCURRENCY=1` to compare with sequential execution.

Run locally:

```powershell
cd qa/browser
npm ci
npx playwright install chromium
npm test
```

`npm run qa:engine` tests the real extension and running local engine with two
files on a locally fulfilled ChatGPT-shaped page. `npm run qa:engine:drop` uses
a synthetic browser drag event instead of the file picker. Neither contacts
the external AI site; the drop test is **not** a native Explorer drag.

For real-site QA, use the separate, git-ignored `.qa-profile`:

```powershell
npm run qa:init       # create profile and load Campfire without opening a window
npm run qa:login      # once: sign in manually in the visible QA browser; close all QA windows afterward
npm run qa:manual -- --sites=claude.ai  # native manual QA with Campfire loaded; close the QA window afterward
npm run qa:preflight  # headless: check engine, editors, and file inputs
npm run qa:live       # headless: scan and approve two synthetic files per site
npm run qa:live:bg    # same run detached, with no browser or terminal window
```

`node live-qa.mjs run --sites=chatgpt.com` limits the run to one site;
`--drop` uses Chromium's input layer to deliver real file paths as drag/drop.
`--headed` runs a minimized window when
headless checks trigger a challenge. `--settle` waits longer for a provider's
upload and response. With `--drop`, `--sequential` dispatches one drop per file
instead of putting both file paths in one drag payload.
`qa:live` can submit test messages and
files to real AI providers. It uses a test phone number only, never user files.
Its report distinguishes a 2xx response to a masked upload request from an
observed request or a file merely visible in the composer. Even a 2xx upload
response does not prove that the provider's assistant processed the file.
The approval uses the real extension worker but does not click the native side
panel, so a headed side-panel smoke test remains separate.

The QA profile holds login sessions and must stay local. Do not copy an active
personal Chrome profile into it or run two browser processes against it at once.
Before `qa:login`, close any previously opened QA browser. The login command now
starts the same browser binary directly, without Playwright automation or remote
debugging; it returns when the QA browser closes. Google may still reject a
Chrome-for-Testing binary. Do not work around Google's sign-in security checks:
use the site's non-Google sign-in option when available, or leave that site's
live test unverified. Do not sign in through the automated headless run.
`qa:preflight:bg` is also available; detached-run logs are saved in
`live-results/`. `qa:login` and `qa:manual` open visible browser windows;
`--headed` may briefly bring a minimized QA window forward.
Human-verification challenges can reappear in headless mode even after a
headed sign-in. The runner reports these as blocked and does not bypass them.
The fixture and engine reports go to `test-results/`; live reports and failure
screenshots go to `live-results/`. Both directories are ignored by Git.

## Pass/fail classification (`run` and `preflight`)

The live run's only hard failure is a security regression: `failed-leak` (an
original filename or byte reached an outbound request). That always sets a
non-zero exit code. `upload-response-ok` and `content-response-ok` are the
verified-pass states.

Everything else is a **blocked / manual-verify** warning that does **not** fail
CI, because it is not a leak and cannot be confirmed by automation: bot
challenges (`human-verification-needed`), `region-blocked`, no upload control
found (`upload-control-needed`), provider upload caps
(`provider-upload-limit`), and `attachment-reinject-failed` — the last is the
extension failing *closed* (it declined to send rather than leak). These reflect
automation limits, not user-facing breakage: a site that blocks Chrome-for-Testing
or renders its composer differently under automation shows here even though the
real browser works (verified for claude.ai's attachment path). Statuses that
usually mean *our* selectors broke (`login-or-selector-needed`, `inconclusive`,
…) still hard-fail. The classification lives in `status-classify.mjs`
(`status-classify.test.mjs` pins it); the summary line marks each site
`ok` / `blocked(manual)` / `FAIL` / `LEAK-FAIL`.
