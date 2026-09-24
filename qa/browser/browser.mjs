import { access, readdir } from 'node:fs/promises';
import path from 'node:path';
import { chromium } from 'playwright';

export async function chromiumExecutable() {
  if (process.env.CAMPFIRE_CHROMIUM_EXECUTABLE) return process.env.CAMPFIRE_CHROMIUM_EXECUTABLE;
  try {
    await access(chromium.executablePath());
    return undefined;
  } catch {
    if (process.platform !== 'win32' || !process.env.LOCALAPPDATA) return undefined;
    const cache = path.join(process.env.LOCALAPPDATA, 'ms-playwright');
    const names = (await readdir(cache).catch(() => [])).filter(name => /^chromium-\d+$/.test(name)).sort().reverse();
    for (const name of names) {
      const candidate = path.join(cache, name, 'chrome-win64', 'chrome.exe');
      try { await access(candidate); console.warn(`Using cached Chromium: ${candidate}`); return candidate; } catch { /* next */ }
    }
    return undefined;
  }
}
