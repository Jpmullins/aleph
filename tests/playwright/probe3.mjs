import { chromium } from "@playwright/test";
const b = await chromium.launch();
const p = await b.newPage();
const errs = [], reqs = [], pageErrs = [];
p.on("console", m => { if (m.type()==="error") errs.push(`[error] ${m.text()}`.slice(0,300)); });
p.on("pageerror", e => pageErrs.push(String(e).slice(0,300)));
p.on("requestfailed", r => reqs.push(`FAILED ${r.method()} ${r.url()} :: ${r.failure()?.errorText}`));
p.on("response", r => { if (r.status() >= 400) reqs.push(`HTTP ${r.status()} ${r.request().method()} ${r.url()}`); });

await p.goto("http://192.168.1.158:5173/", { waitUntil: "networkidle", timeout: 60000 });
await p.getByText("vllm-smoke").first().click().catch(()=>{});
await p.waitForTimeout(6000);
for (const tab of ["Wiki","Library","Notes","Hypotheses","Briefs"]) {
  const el = p.getByRole("button", { name: new RegExp("^"+tab+"$","i") }).first();
  if (await el.count().catch(()=>0)) { await el.click().catch(()=>{}); await p.waitForTimeout(3500); console.log("### tab:", tab); }
}
// try the chat box
const box = p.getByRole("textbox").first();
if (await box.count().catch(()=>0)) {
  await box.fill("hello").catch(()=>{});
  await box.press("Enter").catch(()=>{});
  console.log("### sent chat message");
  await p.waitForTimeout(20000);
}
console.log("\n=== PAGE ERRORS ==="); [...new Set(pageErrs)].forEach(e=>console.log("  "+e));
console.log("=== CONSOLE ERRORS ==="); [...new Set(errs)].slice(0,30).forEach(e=>console.log("  "+e));
console.log("=== NETWORK >=400 / FAILED ==="); [...new Set(reqs)].slice(0,30).forEach(e=>console.log("  "+e));
await b.close();
