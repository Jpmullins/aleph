import { chromium } from "@playwright/test";
const b = await chromium.launch();
const p = await b.newPage();
const errs = [], reqs = [], pageErrs = [];
p.on("console", m => { if (["error","warning"].includes(m.type())) errs.push(`[${m.type()}] ${m.text()}`); });
p.on("pageerror", e => pageErrs.push(String(e)));
p.on("requestfailed", r => reqs.push(`FAILED ${r.method()} ${r.url()} :: ${r.failure()?.errorText}`));
p.on("response", r => { if (r.status() >= 400) reqs.push(`HTTP ${r.status()} ${r.request().method()} ${r.url()}`); });

await p.goto("http://192.168.1.158:5173/", { waitUntil: "networkidle", timeout: 60000 });
await p.getByText("vllm-smoke").first().click().catch(e=>pageErrs.push("click project: "+e.message));
await p.waitForTimeout(8000);
console.log("### URL:", p.url());
console.log("### BODY (1200):\n" + (await p.innerText("body").catch(()=>"<none>")).slice(0,1200));

// try to open any settings-ish control
for (const name of ["Settings","Configure","Models","Model","⚙"]) {
  const el = p.getByRole("button", { name: new RegExp(name,"i") }).first();
  if (await el.count().catch(()=>0)) { await el.click().catch(()=>{}); console.log("### clicked:", name); await p.waitForTimeout(4000); break; }
}
console.log("### AFTER (1000):\n" + (await p.innerText("body").catch(()=>"<none>")).slice(0,1000));
console.log("\n=== PAGE ERRORS ==="); pageErrs.forEach(e=>console.log("  "+e));
console.log("=== CONSOLE ==="); [...new Set(errs)].slice(0,30).forEach(e=>console.log("  "+e));
console.log("=== NETWORK >=400 / FAILED ==="); [...new Set(reqs)].slice(0,30).forEach(e=>console.log("  "+e));
await b.close();
