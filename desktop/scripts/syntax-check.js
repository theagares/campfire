'use strict';
/* scripts/syntax-check.js — 모든 JS 파일에 대해 `node --check` 문법검사 */

const { execFileSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const roots = ['main', 'renderer', 'scripts'];
const files = [];
function walk(dir) {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) walk(p);
    else if (e.name.endsWith('.js')) files.push(p);
  }
}
const base = path.resolve(__dirname, '..');
for (const r of roots) {
  const d = path.join(base, r);
  if (fs.existsSync(d)) walk(d);
}

let failed = 0;
for (const f of files) {
  try {
    execFileSync(process.execPath, ['--check', f], { stdio: 'pipe' });
    console.log('ok  ', path.relative(base, f));
  } catch (err) {
    failed++;
    console.error('FAIL', path.relative(base, f));
    console.error(err.stderr ? err.stderr.toString() : err.message);
  }
}
console.log(`\n${files.length - failed}/${files.length} 통과`);
process.exit(failed ? 1 : 0);
