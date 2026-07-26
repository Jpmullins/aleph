import { chromium } from '@playwright/test';
const browser = await chromium.launch();
const page = await browser.newPage();
const errs = [];
page.on('pageerror', e => errs.push('PAGEERROR: ' + String(e).slice(0,200)));
page.on('console', m => { if (m.type()==='error') errs.push('CONSOLE: ' + m.text().slice(0,200)); });

await page.goto('http://localhost:5173/', { waitUntil: 'networkidle' });
await page.getByText('Neurosymbolic AI & LLM reasoning').first().click();
await page.waitForTimeout(3000);

const txt = (await page.textContent('body')||'').replace(/\s+/g,' ');
console.log('workspace text:', txt.slice(0, 240));

// find a wiki page entry and click it
const candidates = ['Language Model','Chain-of-Thought','Source: '];
let clicked = null;
for (const c of candidates) {
  const loc = page.getByText(c, { exact: false }).first();
  if (await loc.count()) { await loc.click().catch(()=>{}); clicked = c; break; }
}
console.log('clicked:', clicked);
await page.waitForTimeout(2500);
const after = (await page.textContent('body')||'').replace(/\s+/g,' ');
console.log('after click:', after.slice(0, 400));
console.log('ERRORS:', errs.length ? errs.slice(0,8).join(' | ') : '(none)');
await page.screenshot({ path: '/tmp/aleph-run/wiki.png', fullPage: true });
await browser.close();
