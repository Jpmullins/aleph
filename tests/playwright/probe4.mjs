import { chromium } from '@playwright/test';
const browser = await chromium.launch();
const page = await browser.newPage();
const errs = [];
const streams = [];
page.on('pageerror', e => errs.push(String(e).slice(0,200)));
page.on('request', r => { if (r.url().includes('/surfaces/stream')) streams.push(decodeURIComponent(r.url().split('?')[1] || '')); });

await page.goto('http://localhost:5173/', { waitUntil: 'networkidle' });
await page.getByText('AI Reasoning').first().click();
await page.waitForTimeout(3000);
console.log('stream subscriptions BEFORE click:', streams.map(s => s.split('&')[0]));

const before = (await page.textContent('body')||'').replace(/\s+/g,' ');
// click the topic page in the wiki index
const link = page.getByText('AI Reasoning', { exact: false }).nth(1);
console.log('candidate page entries:', await page.getByText('AI Reasoning', {exact:false}).count());
await link.click().catch(e => console.log('click failed', String(e).slice(0,80)));
await page.waitForTimeout(3500);

console.log('stream subscriptions AFTER click:', streams.map(s => s.split('&')[0]));
const after = (await page.textContent('body')||'').replace(/\s+/g,' ');
console.log('reader open (← Wiki back button present)?', after.includes('← Wiki'));
console.log('after sample:', after.slice(0, 300));
console.log('ERRORS:', errs.length ? errs.join(' | ') : '(none)');
await browser.close();
