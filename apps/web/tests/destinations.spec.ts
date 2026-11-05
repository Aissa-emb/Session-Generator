import { test, expect } from "@playwright/test";
import { mkdir } from "node:fs/promises";
import path from "node:path";

test("inline saved values restore, suggest per field, and keep keys private", async ({ page, request, browser, baseURL }) => {
  const keys = ["local-browser-private-1234", "local-symbol-private-5678", "local-staging-private-9876"];
  const before = new Set((await (await request.get('/api/destinations')).json()).destinations.map((r:any)=>r.id));
  const responses: string[] = [];
  page.on("response", async r => { if (r.url().includes("/api/destinations") && r.status() < 400) responses.push(await r.text()); });
  try {
    await page.goto("/");
    await expect(page.getByRole("heading", {name:"Saved destinations"})).toHaveCount(0);
    await expect(page.getByLabel("Destination name", {exact:true})).toHaveCount(0);
    await page.getByRole("button", {name:"iOS", exact:true}).click();
    await page.getByRole("button", {name:"Web", exact:true}).click();
    for (const [label, id] of [["Android", "and01"], ["iOS", "ios01"], ["Web", "web01"]])
      await page.getByLabel(label + " app ID", {exact:true}).fill(id);
    await page.getByLabel("Android app name", {exact:true}).fill("Banking");
    await page.getByLabel("Android ingest region", {exact:true}).selectOption("eu");
    await page.getByLabel("Chronosphere tenant", {exact:true}).fill("tenant-one");
    await page.getByLabel("Chronosphere API token", {exact:true}).fill(keys[0]);
    await page.getByLabel("Symbol Upload token", {exact:true}).fill(keys[1]);
    await page.getByRole("button", {name:"Save now", exact:true}).click();
    await expect(page.getByRole("status")).toContainText("Saved on this computer");
    await expect(page.getByLabel("Chronosphere API token", {exact:true})).toHaveValue("");
    await expect(page.getByText(/Saved ••••1234/)).toBeVisible();
    await expect(page.getByText(/Saved ••••5678/)).toBeVisible();
    await page.reload();
    await expect(page.getByLabel("iOS app ID", {exact:true})).toHaveValue("ios01");
    await expect(page.getByText(/Saved ••••1234/)).toBeVisible();

    // Continue remembers a second combination without a name or separate save flow.
    await page.getByLabel("Android app ID", {exact:true}).fill("and02");
    await page.getByLabel("Android app name", {exact:true}).fill("Retail");
    await page.getByLabel("Android ingest region", {exact:true}).selectOption("us");
    await page.getByLabel("Chronosphere tenant", {exact:true}).fill("tenant-two");
    await expect(page.getByText(/Saved ••••1234/)).toHaveCount(0);
    await page.getByLabel("Chronosphere API token", {exact:true}).fill(keys[2]);
    await page.getByRole("button", {name:"Continue", exact:true}).click();
    await expect(page.getByRole("heading", {name:"Customize traffic"})).toBeVisible();
    await page.getByRole("button", {name:"Back", exact:true}).click();
    await expect(page.getByText(/Saved ••••9876/)).toBeVisible();

    // The app suggestion changes only this platform, and restores its name and region.
    await page.getByLabel("Android app ID", {exact:true}).fill("and0");
    await page.getByRole("option", {name:/and01 Banking/}).click();
    await expect(page.getByLabel("Android app name", {exact:true})).toHaveValue("Banking");
    await expect(page.getByLabel("Android ingest region", {exact:true})).toHaveValue("eu");
    await expect(page.getByLabel("iOS app ID", {exact:true})).toHaveValue("ios01");
    await expect(page.getByLabel("Chronosphere tenant", {exact:true})).toHaveValue("tenant-two");
    await page.getByLabel("Chronosphere tenant", {exact:true}).fill("tenant-on");
    await page.keyboard.press("ArrowDown");
    await page.keyboard.press("Enter");
    await expect(page.getByLabel("Chronosphere tenant", {exact:true})).toHaveValue("tenant-one");
    await expect(page.getByText(/Saved ••••1234/)).toBeVisible();
    await page.getByLabel("Chronosphere API token", {exact:true}).focus();
    await expect(page.getByRole("option", {name:/••••9876 tenant-two/})).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("listbox")).toHaveCount(0);
    // Previously used platform values populate when re-enabled.
    await page.getByRole("button", {name:"iOS", exact:true}).click();
    await page.getByRole("button", {name:"iOS", exact:true}).click();
    await expect(page.getByLabel("iOS app ID", {exact:true})).toHaveValue("ios01");
    await page.getByRole("button", {name:"Save now", exact:true}).click();
    await expect(page.getByRole("status")).toContainText("Saved on this computer");
    const stored = await page.evaluate(() => JSON.stringify({...localStorage, session: {...sessionStorage}}));
    for (const token of keys) { expect(stored).not.toContain(token); expect(responses.join()).not.toContain(token); }

    if (process.env.QA_SCREENSHOTS_DIR) {
      await mkdir(process.env.QA_SCREENSHOTS_DIR, {recursive:true});
      await page.getByLabel("Android app ID", {exact:true}).focus();
      await page.screenshot({path:path.join(process.env.QA_SCREENSHOTS_DIR,"inline-values-desktop.png"),fullPage:true});
      await page.keyboard.press("Escape");
      await page.setViewportSize({width:390,height:844});
      await page.screenshot({path:path.join(process.env.QA_SCREENSHOTS_DIR,"inline-values-mobile.png"),fullPage:true});
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    }
    const context = await browser.newContext({baseURL});
    try {
      const fresh = await context.newPage();
      await fresh.goto("/");
      await expect(fresh.getByLabel("Web app ID", {exact:true})).toHaveValue("web01");
      await expect(fresh.getByText(/Saved ••••1234/)).toBeVisible();
      await fresh.getByRole("button", {name:"Continue",exact:true}).click();
      await expect(fresh.getByRole("heading",{name:"Customize traffic"})).toBeVisible();
      const preview = fresh.waitForRequest(r=>r.url().endsWith("/api/preview") && r.method()==="POST");
      await fresh.getByRole("button",{name:"Generate preview",exact:true}).click();
      const body = (await preview).postDataJSON();
      expect(body.saved_credentials.chronosphere).toMatch(/^[a-f0-9]{32}$/);
      expect(body.saved_credentials.symbol_upload).toMatch(/^[a-f0-9]{32}$/);
      for (const token of keys) expect(JSON.stringify(body)).not.toContain(token);
      await expect(fresh.getByText("Passed", {exact:true})).toBeVisible({timeout:45000});
    } finally { await context.close(); }
    await page.setViewportSize({width:1440,height:1000});
    await page.getByRole("button",{name:"Manage saved values",exact:true}).click();
    const record = page.locator('.remembered-record').filter({hasText:'tenant-one'});
    await record.getByRole("button",{name:"Remove",exact:true}).click();
    await record.getByRole("button",{name:"Remove values and keys",exact:true}).click();
    await expect(record).toHaveCount(0);
    await page.getByRole("button", {name:"Close",exact:true}).click();
    await expect(page.getByRole("status")).toContainText("removed");
  } finally {
    const rows = (await (await request.get('/api/destinations')).json()).destinations;
    for (const row of rows.filter((r:any)=>!before.has(r.id))) await request.delete('/api/destinations/'+row.id);
  }
});

test("failed inline save keeps input for retry and never continues with unsaved credentials", async ({page}) => {
  await page.route('**/api/destinations', async route => {
    if (route.request().method() === 'GET') return route.fulfill({json:{destinations:[], selected_id:null}});
    return route.fulfill({status:502,json:{detail:'The credential could not be saved to Keychain.'}});
  });
  await page.goto('/');
  await page.getByLabel('Android app ID', {exact:true}).fill('and03');
  await page.getByLabel('Chronosphere tenant', {exact:true}).fill('tenant-three');
  await page.getByLabel('Chronosphere API token', {exact:true}).fill('local-unsaved-private-4321');
  await page.getByRole('button', {name:'Continue',exact:true}).click();
  await expect(page.getByRole('alert')).toContainText('could not be saved');
  await expect(page.getByRole('heading', {name:'Choose a destination'})).toBeVisible();
  await expect(page.getByLabel('Chronosphere API token', {exact:true})).toHaveValue('local-unsaved-private-4321');
  expect(await page.evaluate(()=>JSON.stringify({...localStorage}))).not.toContain('local-unsaved-private-4321');
});
