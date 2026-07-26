import { chromium } from '@playwright/test';
const PID = '019f9eef-7b47-7709-966c-0b2d85263ee0';
const browser = await chromium.launch();
const page = await browser.newPage();
const errors = [];
page.on('console', m => { if (m.type() === 'error') errors.push('CONSOLE: ' + m.text().slice(0, 300)); });
page.on('pageerror', e => errors.push('PAGEERROR: ' + String(e).slice(0, 300)));
page.on('requestfailed', r => errors.push('REQFAIL: ' + r.url().slice(0,120) + ' ' + (r.failure()?.errorText ?? '')));

await page.goto('http://localhost:5173/', { waitUntil: 'networkidle' });
await page.waitForTimeout(1500);
console.log('--- landing:', await page.title(), '| body chars:', (await page.textContent('body') || '').length);

// open the project
const proj = page.getByText('[e2e] Context bar').first();
if (await proj.count()) { await proj.click(); await page.waitForTimeout(2500); }
console.log('--- after project click, url:', page.url());

const bodyText = (await page.textContent('body')) || '';
console.log('--- visible text sample:', bodyText.replace(/\s+/g,' ').slice(0, 300));

// Try to find wiki page entries
for (const sel of ['[data-testid="wiki-page-item"]', 'text=Language Model', 'text=Source:']) {
  const loc = page.locator(sel);
  console.log(`--- locator ${sel}: ${await loc.count()} match(es)`);
}
console.log('--- ERRORS ---');
console.log(errors.length ? errors.slice(0, 12).join('\n') : '(none)');
await browser.close();
