import { spawn } from 'node:child_process';
import { closeSync, mkdirSync, openSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const mode = process.argv[2] || 'preflight';
if (!['preflight', 'run'].includes(mode)) throw new Error('Background mode must be preflight or run');

const outputDir = path.join(here, 'live-results');
mkdirSync(outputDir, { recursive: true });
const stamp = new Date().toISOString().replaceAll(':', '-').replaceAll('.', '-');
const logPath = path.join(outputDir, `${mode}-${stamp}.log`);
const errorPath = path.join(outputDir, `${mode}-${stamp}.err.log`);
const out = openSync(logPath, 'w');
const err = openSync(errorPath, 'w');
try {
  const child = spawn(process.execPath, [path.join(here, 'live-qa.mjs'), mode, ...process.argv.slice(3)], {
    cwd: here, detached: true, windowsHide: true, stdio: ['ignore', out, err],
  });
  child.unref();
  console.log(`Background QA PID ${child.pid}`);
  console.log(`Log: ${logPath}`);
  console.log(`Report: ${path.join(outputDir, `${mode}.json`)}`);
  console.log('Do not start another QA run against this profile until this process exits.');
} finally {
  closeSync(out);
  closeSync(err);
}
