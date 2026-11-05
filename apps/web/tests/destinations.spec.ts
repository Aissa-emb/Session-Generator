import { test, expect } from "@playwright/test";
import { mkdir } from "node:fs/promises";
import path from "node:path";

test("saved destinations restore across reload and new browser sessions with masked, isolated keys", async ({ page, request, browser, baseURL }) => {
  const names = [`Saved production ${Date.now()}`, `Saved staging ${Date.now()}`];
  const keys = ["local-browser-private-1234", "local-symbol-private-5678", "local-staging-private-9876"];
  const responses: string[] = [];
  page.on("response", async r => { if (r.url().includes("/api/destinations") && r.status() < 400) responses.push(await r.text()); });
  try {
    await page.goto("/");
    await page.getByLabel("Destination name", {exact:true}).fill(names[0]);
    await page.getByRole("button", {name:"iOS", exact:true}).click();
    await page.getByRole("button", {name:"Web", exact:true}).click();
    for (const [label, id] of [["Android", "and01"], ["iOS", "ios01"], ["Web", "web01"]])
      await page.getByLabel(label + " app ID", {exact:true}).fill(id);
    await page.getByLabel("Chronosphere tenant", {exact:true}).fill("tenant-one");
    await page.getByLabel("Chronosphere API token", {exact:true}).fill(keys[0]);
    await page.getByLabel("Symbol Upload token", {exact:true}).fill(keys[1]);
    await page.getByRole("button", {name:"Save destination", exact:true}).click();
    await expect(page.getByRole("status")).toContainText("Saved on this computer");
    await expect(page.getByLabel("Chronosphere API token", {exact:true})).toHaveValue("");
    await expect(page.getByText(/Saved ••••1234/)).toBeVisible();
    await expect(page.getByText(/Saved ••••5678/)).toBeVisible();
    await page.reload();
    await expect(page.getByLabel("Destination name", {exact:true})).toHaveValue(names[0]);
    await expect(page.getByLabel("iOS app ID", {exact:true})).toHaveValue("ios01");
    await expect(page.getByText(/Saved ••••1234/)).toBeVisible();

    await page.getByRole("button", {name:"New destination", exact:true}).click();
    await page.getByLabel("Destination name", {exact:true}).fill(names[1]);
    await page.getByLabel("Android app ID", {exact:true}).fill("and02");
    await page.getByLabel("Chronosphere tenant", {exact:true}).fill("tenant-two");
    await page.getByLabel("Chronosphere API token", {exact:true}).fill(keys[2]);
    await page.getByRole("button", {name:"Save destination", exact:true}).click();
    await expect(page.getByText(/Saved ••••9876/)).toBeVisible();
    await page.getByLabel("Saved destination", {exact:true}).selectOption({label:names[0]});
    await expect(page.getByLabel("Chronosphere tenant", {exact:true})).toHaveValue("tenant-one");
    await expect(page.getByText(/Saved ••••1234/)).toBeVisible();
    const stored = await page.evaluate(() => JSON.stringify({...localStorage, session: {...sessionStorage}}));
    for (const token of keys) { expect(stored).not.toContain(token); expect(responses.join()).not.toContain(token); }

    if (process.env.QA_SCREENSHOTS_DIR) {
      await mkdir(process.env.QA_SCREENSHOTS_DIR, {recursive:true});
      await page.screenshot({path:path.join(process.env.QA_SCREENSHOTS_DIR,"saved-destinations-desktop.png"),fullPage:true});
      await page.setViewportSize({width:390,height:844});
      await page.screenshot({path:path.join(process.env.QA_SCREENSHOTS_DIR,"saved-destinations-mobile.png"),fullPage:true});
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    }
    const context = await browser.newContext({baseURL});
    try {
      const fresh = await context.newPage();
      await fresh.goto("/");
      await expect(fresh.getByLabel("Destination name", {exact:true})).toHaveValue(names[0]);
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
    await page.getByRole("button",{name:"Remove",exact:true}).click();
    await page.getByRole("button",{name:"Remove destination and keys",exact:true}).click();
    await expect(page.getByRole("status")).toContainText("removed");
  } finally {
    const rows = (await (await request.get('/api/destinations')).json()).destinations;
    for (const row of rows.filter((r:any)=>names.includes(r.name))) await request.delete('/api/destinations/'+row.id);
  }
});
