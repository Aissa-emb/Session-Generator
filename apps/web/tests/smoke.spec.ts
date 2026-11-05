import { test, expect, Page } from "@playwright/test";
import { mkdir } from "node:fs/promises";
import path from "node:path";
async function capture(page: Page, name: string) {
  if (!process.env.QA_SCREENSHOTS_DIR) return;
  await mkdir(process.env.QA_SCREENSHOTS_DIR, { recursive: true });
  await page.screenshot({
    path: path.join(process.env.QA_SCREENSHOTS_DIR, name),
  });
}

async function connect(page: Page, platforms = ["android"]) {
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Choose a destination" }),
  ).toBeVisible();
  for (const p of platforms.filter((p) => p !== "android"))
    await page
      .getByRole("button", { name: p === "ios" ? "iOS" : "Web", exact: true })
      .click();
  if (!platforms.includes("android"))
    await page.getByRole("button", { name: "Android", exact: true }).click();
  for (const p of platforms)
    await page
      .getByLabel(
        (p === "ios" ? "iOS" : p === "web" ? "Web" : "Android") + " app ID",
        { exact: true },
      )
      .fill(p === "ios" ? "ios01" : p === "web" ? "kfs9f" : "5nrc2");
  await page
    .getByRole("button", { name: "Sessions only", exact: false })
    .click();
  if (platforms.join() === "web")
    await capture(page, "redesign-destination.png");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Customize traffic" }),
  ).toBeVisible();
}
async function setRange(page: Page, label: string, value: number) {
  await page.getByLabel(label, { exact: true }).evaluate((el: HTMLInputElement, v) => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(el, String(v));
    el.dispatchEvent(new Event("input", { bubbles: true }));
  }, value);
}
/** Radios are visually hidden behind their labels, so pick the visible card or chip. */
async function pick(page: Page, name: string) {
  await page.locator("label.profile-card, label.chip, label.story-card, label.quick-choice").filter({ hasText: name }).click();
  await expect(page.getByRole("radio", { name: new RegExp(name) })).toBeChecked();
}
async function advanced(page: Page, tab = "Errors") {
  await page.getByRole("button", { name: "Advanced settings", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "Advanced settings" })).toBeVisible();
  await page.getByRole("tab", { name: tab, exact: true }).click();
}
async function preview(page: Page) {
  await page
    .getByRole("button", { name: "Generate preview", exact: true })
    .click();
  await expect(page.getByText("Passed", { exact: true })).toBeVisible({
    timeout: 45000,
  });
  await page
    .getByRole("button", { name: "Continue to send", exact: true })
    .click();
  await page.getByRole("button", { name: "Local test", exact: false }).click();
}

test("error profiles default to production errors, migrate stored drafts once, and imports keep exact mixes", async ({page}) => {
  await connect(page, ["web"]);
  const oldDraft = await page.evaluate(() => {
    const draft = JSON.parse(localStorage.getItem("sg_workflow_v1")!);
    for (const key of ["networkDefaultsVersion", "mixVersion", "profile", "patterns", "severity"]) delete draft[key];
    draft.story = "regression";
    // The version 1 shape: one "pages" signal (LCP) and a top-level LCP target.
    draft.signals = { crashes: false, exceptions: false, freezes: false, network: false, pages: true, payments: false };
    draft.rates = { crashes: 10, exceptions: 15, freezes: 20, network: 80, pages: 30, payments: 30 };
    draft.lcp = 4100;
    localStorage.setItem("sg_workflow_v1", JSON.stringify(draft));
    return draft;
  });
  await page.reload();
  await page.getByRole("button", {name:"Continue",exact:true}).click();
  // A stored draft from before error profiles adopts the default production mix once.
  await expect(page.getByRole("radio", {name:/Production baseline/})).toBeChecked();
  await expect(page.getByRole("button", {name:"Adjust Network failures: 15% of sessions, steady"})).toBeVisible();
  await expect(page.getByRole("img", {name:/^Errors: requested share of sessions over the 5 min run/})).toBeVisible();
  await expect(page.getByRole("img", {name:/^Web Vitals: requested share of sessions over the 5 min run for LCP, INP, CLS, FCP, TTFB/})).toBeVisible();
  const response = page.waitForResponse(r=>r.url().endsWith("/api/preview") && r.request().method()==="POST");
  await page.getByRole("button", {name:"Generate preview",exact:true}).click();
  const result = await response;
  expect(result.ok()).toBeTruthy();
  const sent = result.request().postDataJSON();
  expect(sent.name).toBe("Production baseline · All journeys");
  expect(sent.incidents).toEqual([]);
  const net = sent.client_faults.find((f:any)=>f.type==="network_failure");
  expect(net).toMatchObject({rate:.15,quota:"exact",steer_journey:false});
  expect(Object.fromEntries(sent.client_faults.map((f:any)=>[f.type,f.rate]))).toEqual({js_exception:.2,network_failure:.15,
    lcp_regression:.25,interaction_latency:.15,layout_instability:.15,poor_web_vitals:.1,slow_server_response:.1});
  expect(sent.backend_faults.map((f:any)=>[f.variant,f.rate])).toEqual([["payments-db-pool",.12],["rails-outage",.12]]);
  const payload = await result.json();
  expect(payload.linter).toEqual([]);
  expect(payload.report.faults.find((f:any)=>f.id===net.id)).toMatchObject({emitted_sessions:15,within_tolerance:true});
  // Web vital ratings also reflect natural page speed, so their shares are reported, not verified.
  for (const id of ["lcp-web","inp-web","cls-web","fcp-web","ttfb-web"])
    expect(payload.report.faults.find((f:any)=>f.id===id)).toMatchObject({natural_overlap:true,within_tolerance:null,evidence:"web vitals"});
  await expect(page.getByText("incl. natural").first()).toBeVisible({timeout:45000});
  await expect(page.getByText(/Web Vitals rows count any session with a non-good rating/)).toBeVisible();
  await page.getByRole("button", {name:"Back",exact:true}).click();
  await page.getByRole("button", {name:"Adjust signals",exact:true}).click();
  await page.getByLabel("Network failures share of sessions", {exact:true}).focus();
  await page.keyboard.press("Home");
  await expect(page.getByLabel("Network failures share of sessions", {exact:true})).toHaveValue("0");
  await page.getByRole("button", {name:"Done",exact:true}).click();
  await expect(page.getByText("Custom mix", {exact:true})).toBeVisible();
  await page.reload();
  await page.getByRole("button", {name:"Continue",exact:true}).click();
  await expect(page.getByText("Off: Network failures", {exact:true})).toBeVisible();
  await expect(page.getByRole("radio", {name:/Production baseline/})).not.toBeChecked();
  await advanced(page);
  await page.locator("input[type=file]").setInputFiles({name:"older-workflow.json",mimeType:"application/json",buffer:Buffer.from(JSON.stringify({format:"vantra-workflow",version:1,draft:oldDraft}))});
  await expect(page.getByText("Configuration opened.", {exact:true})).toBeVisible();
  // Imports keep their exact signals as a custom mix (older files adopt network failures once).
  await expect(page.getByLabel("Network failures share of sessions", {exact:true})).toHaveValue("15");
  await expect(page.getByLabel("LCP share of sessions", {exact:true})).toHaveValue("30");
  await expect(page.getByLabel("LCP p75 on regressed pages", {exact:true})).toHaveValue("4100");
  await expect(page.locator("#signal-exceptions")).toContainText("Off");
  // Vitals the old file never had stay off.
  for (const k of ["inp","cls","fcp","ttfb"]) await expect(page.locator(`#signal-${k} .signal-value`)).toHaveText("Off");
  await page.keyboard.press("Escape");
  await expect(page.getByRole("button", {name:"Adjust LCP: 30% of sessions, steady"})).toBeVisible();
  await pick(page, "Diagnostic story");
  await expect(page.getByRole("heading", {name:"Signal mix"})).toHaveCount(0);
  await expect(page.getByRole("group", {name:"Story"})).toBeVisible();
  await expect(page.getByRole("radio", {name:/Sign-in pool timeout.*503/})).toBeVisible();
  await pick(page, "Scheduled incident");
  await expect(page.getByText("Healthy before the window, failing during it, recovered after.")).toBeVisible();
  await expect(page.getByLabel("Incident starts", {exact:true})).toBeVisible();
});

test("signal patterns send smooth incidents placed in the run window", async ({page}) => {
  await connect(page, ["android", "web"]);
  await pick(page, "Incident spike");
  await page.getByRole("button", {name:"Continuous sending",exact:true}).click();
  await expect(page.getByRole("img", {name:/^Errors: requested share of sessions over the 60 min run/})).toBeVisible();
  await expect(page.getByRole("button", {name:"Adjust Network failures: 8–45% of sessions, spike"})).toBeVisible();
  // Keyboard: the legend opens the sheet on that signal's slider; tabs use arrow keys; Escape returns focus.
  const legend = page.getByRole("button", {name:/^Adjust Crashes/});
  await legend.click();
  await expect(page.getByLabel("Crashes share of sessions", {exact:true})).toBeFocused();
  await page.keyboard.press("ArrowRight");
  await expect(page.locator("#signal-crashes .signal-value")).toHaveText("7%");
  await page.getByRole("group", {name:"Crashes pattern"}).getByRole("button", {name:"Rise",exact:true}).click();
  await setRange(page, "Crashes rise start", 50);
  await expect(page.locator("#signal-crashes")).toContainText("Starts at 30 min");
  await page.getByRole("tab", {name:"Errors",exact:true}).focus();
  await page.keyboard.press("ArrowRight");
  await expect(page.getByRole("tab", {name:"Visitors",exact:true})).toBeFocused();
  await expect(page.getByRole("tabpanel")).toContainText("Returning visitors");
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByRole("button", {name:/^Adjust Crashes/})).toBeFocused();
  await expect(page.getByText("Custom mix", {exact:true})).toBeVisible();
  const before = Date.now();
  const response = page.waitForResponse(r=>r.url().endsWith("/api/preview") && r.request().method()==="POST");
  await page.getByRole("button", {name:"Generate preview",exact:true}).click();
  const result = await response;
  expect(result.ok()).toBeTruthy();
  const sent = result.request().postDataJSON();
  const byFault = Object.fromEntries(sent.incidents.map((i:any)=>[i.fault_ref,i]));
  expect(Object.keys(byFault).sort()).toEqual(["crashes-android","exceptions-android","exceptions-web","lcp-web","network-android","network-web","payments-db-pool","rails-outage","ttfb-web"]);
  expect(byFault["network-web"]).toMatchObject({curve:"smooth",peak_rate:.45,baseline_rate:.08});
  // A 24-minute spike centred 30 minutes into the run.
  const centre = Date.parse(byFault["network-web"].start) + (byFault["network-web"].onset_minutes + byFault["network-web"].peak_minutes / 2) * 60000;
  expect(Math.abs(centre - before - 30 * 60000)).toBeLessThan(60000);
  expect(byFault["crashes-android"]).toMatchObject({curve:"smooth",peak_rate:.07,baseline_rate:.01,recovery_minutes:0});
  expect(Math.abs(Date.parse(byFault["crashes-android"].start) - before - 30 * 60000)).toBeLessThan(60000);
  expect(byFault["payments-db-pool"]).not.toHaveProperty("baseline_rate");
  const payload = await result.json();
  expect(payload.linter).toEqual([]);
  await expect(page.getByText("Passed", {exact:true})).toBeVisible({timeout:45000});
  await page.getByRole("button", {name:"Continue to send",exact:true}).click();
  await expect(page.getByText("Crashes (1–7%, rise)", {exact:false})).toBeVisible();
});

test("each web vital has its own share, pattern and severity in the sent spec", async ({page}) => {
  await connect(page, ["web"]);
  await page.getByRole("button", {name:"Adjust signals",exact:true}).click();
  await expect(page.getByRole("heading", {name:"Errors",exact:true})).toBeVisible();
  await expect(page.getByRole("heading", {name:"Web Vitals",exact:true})).toBeVisible();
  for (const [k, readout] of [["lcp","LCP p75 3.8 s"],["inp","INP p75 450 ms"],["cls","CLS p75 0.30"],["fcp","FCP p75 3.2 s"],["ttfb","TTFB p75 2200 ms"]]) {
    await expect(page.locator(`#signal-${k}`)).toContainText(readout);
    await expect(page.getByRole("group", {name:`${k.toUpperCase()} pattern`})).toBeVisible();
  }
  await expect(page.locator("#signal-inp")).toContainText("Chromium only");
  await setRange(page, "INP p75 on regressed pages", 800);
  await setRange(page, "CLS p75 on regressed pages", 0.5);
  await setRange(page, "TTFB p75 on regressed pages", 3000);
  await setRange(page, "FCP share of sessions", 0);
  await expect(page.locator("#signal-inp")).toContainText("INP p75 800 ms");
  await page.getByRole("group", {name:"LCP pattern"}).getByRole("button", {name:"Spike",exact:true}).click();
  await page.getByRole("button", {name:"Done",exact:true}).click();
  await expect(page.getByRole("button", {name:"Adjust LCP: 5–25% of sessions, spike"})).toBeVisible();
  await expect(page.getByRole("button", {name:/^Adjust INP: 15% of sessions/})).toContainText("p75 800 ms");
  await expect(page.getByText("Off: FCP", {exact:true})).toBeVisible();
  const response = page.waitForResponse(r=>r.url().endsWith("/api/preview") && r.request().method()==="POST");
  await page.getByRole("button", {name:"Generate preview",exact:true}).click();
  const result = await response;
  expect(result.ok()).toBeTruthy();
  const sent = result.request().postDataJSON();
  const byId = Object.fromEntries(sent.client_faults.map((f:any)=>[f.id,f]));
  expect(byId["lcp-web"]).toMatchObject({type:"lcp_regression",rate:.25,details:{p75_ms:3800,cause:"hero_image"}});
  expect(byId["inp-web"]).toMatchObject({type:"interaction_latency",rate:.15,details:{p75_ms:800}});
  expect(byId["cls-web"]).toMatchObject({type:"layout_instability",rate:.15,details:{p75:.5}});
  expect(byId["ttfb-web"]).toMatchObject({type:"slow_server_response",rate:.1,details:{p75_ms:3000}});
  expect(byId["fcp-web"]).toBeUndefined();
  expect(sent.incidents.map((i:any)=>i.fault_ref)).toEqual(["lcp-web"]);
  expect((await result.json()).linter).toEqual([]);
  await expect(page.getByText("Passed", {exact:true})).toBeVisible({timeout:45000});
  await page.getByRole("button", {name:"Back",exact:true}).click();
  // FCP comes from the generic vitals target.
  await page.getByRole("button", {name:"Adjust signals",exact:true}).click();
  await setRange(page, "FCP share of sessions", 20);
  await setRange(page, "FCP p75 on regressed pages", 4000);
  await page.getByRole("button", {name:"Done",exact:true}).click();
  const again = page.waitForResponse(r=>r.url().endsWith("/api/preview") && r.request().method()==="POST");
  await page.getByRole("button", {name:"Generate preview",exact:true}).click();
  const fcp = (await again).request().postDataJSON().client_faults.find((f:any)=>f.id==="fcp-web");
  expect(fcp).toMatchObject({type:"poor_web_vitals",rate:.2,details:{targets:{fcp:4000}}});
});

test("version 2 drafts map slow page loads to LCP and take new vitals from their profile", async ({page}) => {
  await connect(page, ["android", "web"]);
  await page.evaluate(() => {
    const draft = JSON.parse(localStorage.getItem("sg_workflow_v1")!);
    draft.mixVersion = 2;
    draft.profile = "healthy";
    delete draft.severity;
    for (const field of ["signals", "rates", "patterns"]) for (const k of ["lcp","inp","cls","fcp","ttfb"]) delete draft[field][k];
    draft.signals.pages = true;
    draft.rates.pages = 12;
    draft.patterns.pages = { shape: "spike", peakAt: 50, width: 40, baseline: 4, waves: 3 };
    draft.lcp = 5000;
    localStorage.setItem("sg_workflow_v1", JSON.stringify(draft));
  });
  await page.reload();
  await page.getByRole("button", {name:"Continue",exact:true}).click();
  await expect(page.getByRole("button", {name:"Adjust LCP: 4–12% of sessions, spike"})).toContainText("p75 5.0 s");
  // Healthy's own levels and near-threshold severities fill the vitals version 2 never had.
  await expect(page.getByRole("button", {name:"Adjust INP: 3% of sessions, steady"})).toContainText("p75 260 ms");
  await expect(page.getByRole("button", {name:"Adjust TTFB: 3% of sessions, steady"})).toContainText("p75 950 ms");
  const stored = await page.evaluate(() => JSON.parse(localStorage.getItem("sg_workflow_v1")!));
  expect(stored.mixVersion).toBe(3);
  expect(stored.signals.pages).toBeUndefined();
  expect(stored.lcp).toBeUndefined();
});

test("web vitals are unavailable without a web app", async ({page}) => {
  await connect(page, ["android"]);
  await expect(page.getByRole("heading", {name:"Web Vitals",exact:true})).toHaveCount(0);
  await page.getByRole("button", {name:"Adjust signals",exact:true}).click();
  for (const k of ["lcp","inp","cls","fcp","ttfb"]) {
    await expect(page.locator(`#signal-${k}`)).toContainText("Needs a web app");
    await expect(page.locator(`#signal-${k} .signal-value`)).toHaveText("N/A");
  }
});

test("runs longer than a day send steady shares instead of timed patterns", async ({page}) => {
  await connect(page, ["web"]);
  await pick(page, "Incident spike");
  await page.getByRole("button", {name:"Continuous sending",exact:true}).click();
  await page.getByLabel("Run for", {exact:true}).selectOption("custom");
  await page.getByLabel("Duration (minutes)", {exact:true}).fill("2000");
  await expect(page.getByRole("button", {name:"Adjust Network failures: 8–45% of sessions, spike"})).toBeVisible();
  await page.getByRole("button", {name:"Adjust signals",exact:true}).click();
  await expect(page.getByRole("group", {name:"Network failures pattern"}).getByRole("button", {name:"Spike",exact:true})).toBeDisabled();
  await expect(page.locator("#signal-network")).toContainText("Patterns apply to runs of up to 24 hours");
  await page.keyboard.press("Escape");
  const response = page.waitForResponse(r=>r.url().endsWith("/api/preview") && r.request().method()==="POST");
  await page.getByRole("button", {name:"Generate preview",exact:true}).click();
  const sent = (await response).request().postDataJSON();
  expect(sent.incidents).toEqual([]);
  expect(sent.client_faults.find((f:any)=>f.type==="network_failure").rate).toBe(.45);
});

test("bad release and flaky network batches stay inside the five-minute window", async ({page}) => {
  await connect(page, ["android", "web"]);
  // Bad release ramps crashes, exceptions (Android and web), freezes, INP and CLS;
  // Flaky network repeats network failures (Android and web) and web TTFB.
  for (const [profile, faults] of [["Bad release", 6], ["Flaky network", 3]] as const) {
    await pick(page, profile);
    const before = Date.now();
    const response = page.waitForResponse(r=>r.url().endsWith("/api/preview") && r.request().method()==="POST");
    await page.getByRole("button", {name:"Generate preview",exact:true}).click();
    const result = await response;
    expect(result.ok()).toBeTruthy();
    const sent = result.request().postDataJSON();
    expect(sent.name).toBe(profile + " · All journeys");
    expect(sent.incidents).toHaveLength(faults);
    for (const i of sent.incidents) {
      const start = Date.parse(i.start);
      expect(start).toBeGreaterThanOrEqual(before - 5 * 60000 - 2000);
      expect(start).toBeLessThan(before + 1000);
    }
    if (profile === "Flaky network") {
      expect(sent.incidents.every((i:any)=>i.repeat_every_minutes > 0)).toBeTruthy();
      expect(sent.modeling.bindings.map((b:any)=>b.quantity)).toEqual(["network_rtt_ms"]);
    }
    expect((await result.json()).linter).toEqual([]);
    await expect(page.getByText("Passed", {exact:true})).toBeVisible({timeout:45000});
    await page.getByRole("button", {name:"Back",exact:true}).click();
  }
  await expect(page.getByText("Also: Slow network", {exact:false})).toBeVisible();
  await pick(page, "Healthy");
  await expect(page.getByText("Also: Slow network", {exact:false})).toHaveCount(0);
});

test("varied visitor model reaches preview with native lifecycle controls", async ({page}, testInfo) => {
  await connect(page, ["android", "ios", "web"]);
  await advanced(page, "Visitors");
  await expect(page.getByLabel("Visit patterns", {exact:true})).toHaveValue("varied");
  await setRange(page, "Returning visitors", 40);
  await setRange(page, "Cold starts on return", 30);
  await page.getByRole("button", {name:"Done",exact:true}).click();
  const response = page.waitForResponse(r=>r.url().endsWith("/api/preview") && r.request().method()==="POST");
  await page.getByRole("button", {name:"Generate preview",exact:true}).click();
  const result = await response;
  expect(result.ok()).toBeTruthy();
  const data = await result.json();
  expect(data.report.population.length).toBeGreaterThan(0);
  expect(result.request().postDataJSON().population.model).toBe("varied");
  expect(result.request().postDataJSON().population.returning_share).toBe(.4);
  await page.getByText("Visitor distribution · this sample", {exact:true}).click();
  await expect(page.getByRole("columnheader", {name:"Visit intent",exact:true})).toBeVisible();
  await page.screenshot({path:testInfo.outputPath("population-preview.png"),fullPage:true});
});

test("one workflow, input validation, scoped platform controls, no AI or MCP", async ({
  page,
  request,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("/");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("five-character app ID");
  await connect(page, ["web"]);
  await capture(page, "redesign-customize.png");
  await expect(page.getByRole("button", { name: /^Adjust Crashes/ })).toHaveCount(0);
  await advanced(page);
  await expect(page.locator("#signal-crashes")).toContainText("Needs an Android or iOS app");
  await expect(page.getByLabel("Crashes share of sessions", { exact: true })).toHaveCount(0);
  await expect(page.getByLabel("LCP share of sessions", { exact: true })).toHaveValue("25");
  await setRange(page, "LCP share of sessions", 30);
  await expect(page.locator("#signal-lcp .signal-value")).toHaveText("30%");
  await capture(page, "redesign-advanced.png");
  await expect(page.getByText(/copilot|MCP connection/i)).toHaveCount(0);
  expect((await request.post("/mcp", { data: {} })).status()).toBe(404);
  expect(
    (await request.post("/api/copilot/turns", { data: {} })).status(),
  ).toBe(404);
  expect(errors).toEqual([]);
});

test("custom mixed-platform faults produce real session and distributed trace previews", async ({
  page,
  context,
}) => {
  await connect(page, ["android", "web", "ios"]);
  await page.getByRole("button", { name: "Back", exact: true }).click();
  await page
    .getByRole("button", {
      name: "Sessions + distributed traces",
      exact: false,
    })
    .click();
  await page
    .getByLabel("Chronosphere tenant", { exact: true })
    .fill("https://preview-tenant.chronosphere.io");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await pick(page, "Shopping & checkout");
  // Every signal is on by default.
  for (const name of ["Crashes", "Exceptions", "App freezes", "Network failures", "Payment failures", "LCP", "INP", "CLS", "FCP", "TTFB"])
    await expect(page.getByRole("button", { name: new RegExp("^Adjust " + name + ":") })).toBeVisible();
  await page.getByLabel("Number of sessions").fill("24");
  await page
    .getByRole("button", { name: "Generate preview", exact: true })
    .click();
  await expect(page.getByText("Passed", { exact: true })).toBeVisible({
    timeout: 45000,
  });
  await expect(
    page.getByRole("heading", { name: "Included signals" }),
  ).toBeVisible();
  await expect(page.locator(".waterfall-row").first()).toBeVisible();
  for (const name of ["Android", "iOS", "Web"]) {
    await page
      .getByRole("group", { name: "Preview platform" })
      .getByRole("button", { name, exact: true })
      .click();
    await expect(page.locator(".timeline-event").first()).toBeVisible();
  }
  await capture(page, "redesign-preview.png");
  const eventCount = await page.locator(".timeline-event").count();
  await page
    .getByRole("searchbox", { name: "Filter session events" })
    .fill("no-such-operation");
  await expect(page.locator(".timeline-event")).toHaveCount(0);
  await expect(page.getByText("No events match this filter.")).toBeVisible();
  await page.getByRole("searchbox", { name: "Filter session events" }).clear();
  await expect(page.locator(".timeline-event")).toHaveCount(eventCount);
  const row = page.locator(".waterfall-row").first();
  const service = (await row.locator("strong").innerText()).trim();
  const color = await row
    .locator(".service-dot")
    .evaluate((el) => getComputedStyle(el).backgroundColor);
  await page
    .getByRole("searchbox", { name: "Filter trace spans" })
    .fill(service);
  for (const text of await page
    .locator(".waterfall-row strong")
    .allTextContents())
    expect(text).toContain(service);
  expect(
    await row
      .locator(".service-dot")
      .evaluate((el) => getComputedStyle(el).backgroundColor),
  ).toBe(color);
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await row.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(
    page.getByRole("dialog").locator(".inspector-grid"),
  ).toContainText(service);
  await page.getByRole("button", { name: "Copy payload" }).click();
  await expect(
    page.getByRole("status").filter({ hasText: "Copied" }),
  ).toBeVisible();
  const copied = JSON.parse(
    await page.evaluate(() => navigator.clipboard.readText()),
  );
  expect(copied.service).toBe(service);
  await page.getByText("Raw payload", { exact: true }).click();
  await expect(page.getByRole("dialog").locator("pre")).toContainText(
    copied.name,
  );
  await capture(page, "refined-span-inspector.png");
  await page.keyboard.press("Escape");
  await expect(row).toBeFocused();
  await page.getByRole("searchbox", { name: "Filter trace spans" }).clear();
  await page
    .getByRole("heading", { name: "Session timeline" })
    .scrollIntoViewIfNeeded();
  await capture(page, "refined-preview.png");
  await page.getByRole("button", { name: "Continue to send" }).click();
  await expect(
    page.getByRole("button", { name: "Send batch", exact: true }),
  ).toBeDisabled();
});

test("dropdown arrows stay centered and inset with native keyboard operation", async ({
  page,
}) => {
  await page.goto("/");
  const select = page.getByLabel("Android ingest region", { exact: true });
  for (const fontSize of [16, 32]) {
    await page.evaluate(
      (size) => (document.documentElement.style.fontSize = `${size}px`),
      fontSize,
    );
    for (const width of [320, 390, 780, 1100, 1440]) {
      await page.setViewportSize({ width, height: 900 });
      await select.focus();
      const layout = await select.evaluate((el) => {
        const control = el.getBoundingClientRect();
        const arrow = el
          .parentElement!.querySelector("svg")!
          .getBoundingClientRect();
        return {
          inset: control.right - arrow.right,
          offset: Math.abs(
            (control.top + control.bottom) / 2 - (arrow.top + arrow.bottom) / 2,
          ),
          textPadding: Number.parseFloat(getComputedStyle(el).paddingRight),
          arrowWidth: arrow.width,
        };
      });
      expect(layout.inset).toBeGreaterThanOrEqual(12);
      expect(layout.offset).toBeLessThan(1);
      expect(layout.textPadding).toBeGreaterThan(
        layout.arrowWidth + layout.inset + 6,
      );
    }
  }
  await page.evaluate(() => (document.documentElement.style.fontSize = "16px"));
  await select.press("e");
  await select.press("Tab");
  await expect(select).toHaveValue("eu");
  await select.press("u");
  await select.press("Tab");
  await expect(select).toHaveValue("us");
  await select.focus();
  await capture(page, "refined-select-focus.png");
});

test("complete local batch records receipts and survives refresh", async ({
  page,
}) => {
  await connect(page);
  await page.getByLabel("Number of sessions").fill("4");
  await preview(page);
  await page
    .getByRole("button", { name: "Start local test", exact: true })
    .click();
  await expect(
    page.locator(".run-status").filter({ hasText: /^Completed$/ }),
  ).toBeVisible({ timeout: 45000 });
  await expect(page.locator(".run-metrics").first()).toContainText("4");
  await expect(
    page.getByRole("cell", { name: "Generated", exact: true }).first(),
  ).toBeVisible();
  await page.reload();
  await expect(
    page.locator(".run-status").filter({ hasText: /^Completed$/ }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Create another run" }).click();
  await expect(page.getByLabel("Android app ID", { exact: true })).toHaveValue(
    "5nrc2",
  );
});

test("continuous rate and two-hour duration start a real local worker with pause, resume and stop", async ({
  page,
}) => {
  test.setTimeout(120000);
  // Keep the real worker and drain behavior, with a reproducible journey rather
  // than a randomly long session occasionally exceeding the assertion timeout.
  await page.route("**/api/specs", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    const spec = route.request().postDataJSON();
    await route.continue({ postData: JSON.stringify({ ...spec, seed: 7331 }) });
  });
  await connect(page);
  await page
    .getByRole("button", { name: "Continuous sending", exact: true })
    .click();
  await page.getByLabel("Sessions per minute", { exact: true }).fill("10");
  await page.getByLabel("Run for", { exact: true }).selectOption("120");
  await expect(page.getByText("1,200 sessions", { exact: true })).toBeVisible();
  await preview(page);
  await expect(page.getByText("10 sessions/min for 120 minutes")).toBeVisible();
  await page
    .getByRole("button", { name: "Start local test", exact: true })
    .click();
  try {
    await expect(
      page.locator(".run-status").filter({ hasText: /^Running$/ }),
    ).toBeVisible({ timeout: 45000 });
    await expect(
      page.getByRole("img", { name: "Live delivery activity", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("progressbar", { name: "Scheduled time elapsed" }),
    ).toHaveAttribute("max", "7200");
    await page.reload();
    await expect(
      page.locator(".run-status").filter({ hasText: /^Running$/ }),
    ).toBeVisible();
    await capture(page, "redesign-local-run.png");
    await page.getByRole("button", { name: "Pause", exact: true }).click();
    await expect(
      page.locator(".run-status").filter({ hasText: /Pausing|Paused/ }),
    ).toBeVisible();
    await expect(page.locator(".run-status")).toHaveText("Paused", {
      timeout: 90000,
    });
    // Counts now represent completed RUM delivery, not session creation.
    await expect(
      page.locator(".run-metrics>div").first().locator("dd"),
    ).toHaveText(/^[1-9][\d,]*$/);
    await page.getByRole("button", { name: "Resume", exact: true }).click();
    await expect(page.locator(".run-status")).toHaveText("Running");
    await page.getByRole("button", { name: "Stop", exact: true }).click();
    await expect(
      page.locator(".run-status").filter({ hasText: /^Stopped$/ }),
    ).toBeVisible({ timeout: 45000 });
  } finally {
    const id = await page.evaluate(() =>
      JSON.parse(localStorage.getItem("sg_run_id") || "null"),
    );
    if (id) await page.request.post("/api/runs/" + id + "/stop");
  }
});

test("failed and partially skipped runs say why without exposing raw errors", async ({
  page,
}) => {
  const base = {
    id: "observed-run",
    spec_id: "observed-spec",
    spec_version: 1,
    mode: "live",
    destination: "sessions_only",
    dry_run: true,
    desired_status: "running",
    started: Date.now() / 1000 - 145,
    ended: null,
    heartbeat: Date.now() / 1000,
    counters: {
      sessions: 60,
      payloads: 927,
      accepted: 8606,
      rejected: 0,
      errors: 0,
    },
    delivery: {},
    receipts: [],
    verifications: [],
    report: {},
  };
  let run: Record<string, unknown> = {
    ...base,
    status: "running",
    skipped_sessions: 1,
    last_skipped: {
      type: "KeyError",
      detail: null,
      where: ["xcorgen/browser_session.py:612 in _request"],
    },
  };
  await page.route("**/api/runs/observed-run", (r) => r.fulfill({ json: run }));
  await page.route("**/api/specs/observed-spec*", (r) =>
    r.fulfill({
      json: {
        id: "observed-spec",
        version: 1,
        spec: {
          name: "Regression · All journeys",
          targets: [
            {
              id: "vantra-web",
              platform: "web",
              app_name: "Vantra Web",
              embrace_app_id: "kfs9f",
            },
          ],
          traffic: { mode: "live", rate_per_minute: 20, duration_minutes: 120 },
          destinations: { mode: "sessions_only" },
        },
      },
    }),
  );
  await page.addInitScript(() =>
    localStorage.setItem("sg_run_id", JSON.stringify("observed-run")),
  );
  await page.goto("/");
  const skipped = page
    .getByRole("status")
    .filter({ hasText: "1 session was skipped." });
  await expect(skipped).toContainText(
    "Nothing from it was sent; the run continues.",
  );
  await expect(skipped).toContainText(
    "Last reason: unexpected KeyError at xcorgen/browser_session.py:612 in _request.",
  );
  run = {
    ...base,
    status: "failed",
    ended: Date.now() / 1000,
    skipped_sessions: 3,
    failure: {
      type: "JobFailure",
      where: ["server/worker.py:225 in live"],
      detail:
        "3 sessions in a row could not be generated, so the stream stopped. Last: Generated session failed the realism linter (pii at $.embrace.logs.resourceLogs[0].message).",
    },
  };
  const alert = page
    .getByRole("alert")
    .filter({ hasText: "The run needs attention." });
  await expect(alert).toContainText(
    "Reason: 3 sessions in a row could not be generated, so the stream stopped.",
  );
  await expect(page.getByText("1 session was skipped.")).toHaveCount(0);
  await expect(page.locator(".run-status")).toHaveText("Needs attention");
});

test("offline detection explains recovery, retains draft, and reconnects", async ({
  page,
}) => {
  await connect(page);
  await page.getByLabel("Number of sessions").fill("17");
  await page.route("**/api/status", (r) => r.abort());
  await page.route("**/api/preview", (r) => r.abort());
  await page
    .getByRole("button", { name: "Generate preview", exact: true })
    .click();
  await expect(
    page.getByRole("alert").filter({ hasText: "Couldn’t complete" }),
  ).toContainText("Start Session Generator.command");
  await expect(page.getByText("Offline", { exact: true })).toBeVisible({
    timeout: 10000,
  });
  await page.unroute("**/api/status");
  await page.unroute("**/api/preview");
  await page.getByRole("button", { name: "Reconnect", exact: true }).click();
  await expect(page.getByText("Connected", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Back", exact: true }).click();
  await expect(page.getByLabel("Number of sessions")).toHaveValue("17");
  await page
    .getByRole("button", { name: "Generate preview", exact: true })
    .click();
  await expect(page.getByText("Passed", { exact: true })).toBeVisible({
    timeout: 45000,
  });
});

test("lost start response recovers the same run after refresh", async ({
  page,
  request,
}) => {
  await connect(page);
  await page.getByLabel("Number of sessions").fill("2");
  await preview(page);
  let created = "";
  await page.route("**/api/runs", async (route) => {
    if (route.request().method() === "POST") {
      const { spec_id } = route.request().postDataJSON();
      const correction = await request.patch(`/api/specs/${spec_id}`, {
        data: {
          expected_version: 1,
          patch: [
            {
              op: "replace",
              path: "/targets/0/embrace_app_id",
              value: "5nrc3",
            },
          ],
        },
      });
      expect(correction.ok()).toBeTruthy();
      const response = await route.fetch();
      created = (await response.json()).id;
      await route.abort();
    } else await route.continue();
  });
  await page
    .getByRole("button", { name: "Start local test", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Retry start request" }),
  ).toBeVisible();
  await page.unroute("**/api/runs");
  await page.reload();
  await expect(
    page.getByRole("button", { name: "Local test", exact: false }),
  ).toHaveAttribute("aria-pressed", "true");
  await expect(
    page.getByText("Vantra · Android (5nrc3)", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Retry start request" }).click();
  await expect(
    page.locator(".run-status").filter({ hasText: /^Completed$/ }),
  ).toBeVisible({ timeout: 45000 });
  const id = await page.evaluate(() =>
    JSON.parse(localStorage.getItem("sg_run_id") || "null"),
  );
  expect(id).toBe(created);
  const runs = await (await request.get("/api/runs")).json();
  expect(runs.filter((r: any) => r.id === created)).toHaveLength(1);
  await page.getByRole("button", { name: "Create another run" }).click();
  await expect(page.getByLabel("Android app ID", { exact: true })).toHaveValue(
    "5nrc3",
  );
});

test("a definitive credential preflight error unlocks Destination for correction", async ({
  page,
}) => {
  await connect(page);
  await page.getByLabel("Number of sessions").fill("2");
  await preview(page);
  await page.route("**/api/runs", async (route) => {
    if (route.request().method() === "POST")
      await route.fulfill({
        status: 422,
        json: {
          detail:
            "The Cortex XCOR token could not be read. Save it again in Destination. No run was started.",
        },
      });
    else await route.continue();
  });
  await page
    .getByRole("button", { name: "Start local test", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText("No run was started");
  await expect(
    page.getByRole("button", { name: "Retry start request" }),
  ).toHaveCount(0);
  expect(
    await page.evaluate(() => localStorage.getItem("sg_launch_attempt")),
  ).toBeNull();
  await page.getByRole("button", { name: /Destination/ }).click();
  await expect(
    page.getByRole("heading", { name: "Choose a destination" }),
  ).toBeVisible();
});

test("laptop and mobile layout stay within the viewport", async ({ page }) => {
  await connect(page, ["android", "web"]);
  for (const size of [
    { width: 1280, height: 900 },
    { width: 390, height: 844 },
  ]) {
    await page.setViewportSize(size);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBeTruthy();
  }
  await page.getByRole("button", { name: "Back", exact: true }).click();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBeTruthy();
});

test("platform selector keeps icons, labels and checkmarks contained through resizing", async ({
  page,
}) => {
  await page.goto("/");
  await page.evaluate(() => document.fonts.ready);
  const buttons = page.locator(".platform-picker button");
  for (const fontSize of [16, 32]) {
    await page.evaluate((size) => {
      document.documentElement.style.fontSize = `${size}px`;
    }, fontSize);
    for (const width of [390, 640, 780, 781, 1100, 1440, 320, 1920]) {
      await page.setViewportSize({ width, height: width % 2 ? 900 : 600 });
      let previous: { box: number[]; icon: number[] }[] | undefined;
      for (const selection of [[0], [0, 1, 2], [2]]) {
        // Add before removing so one platform always remains selected.
        for (const wanted of [true, false]) {
          for (let i = 0; i < 3; i++) {
            if (selection.includes(i) !== wanted) continue;
            if (
              ((await buttons.nth(i).getAttribute("aria-pressed")) ===
                "true") !==
              wanted
            )
              await buttons.nth(i).click();
          }
        }
        const layout = await buttons.evaluateAll((items) =>
          items.map((button) => {
            const rect = button.getBoundingClientRect();
            const icon = button.querySelector("svg")!.getBoundingClientRect();
            const contents = [...button.querySelectorAll("svg")]
              .filter((el) => getComputedStyle(el).visibility !== "hidden")
              .map((el) => el.getBoundingClientRect());
            const walker = document.createTreeWalker(
              button,
              NodeFilter.SHOW_TEXT,
            );
            while (walker.nextNode()) {
              if (!walker.currentNode.textContent?.trim()) continue;
              const range = document.createRange();
              range.selectNodeContents(walker.currentNode);
              contents.push(range.getBoundingClientRect());
            }
            return {
              box: [
                rect.x + scrollX,
                rect.y + scrollY,
                rect.width,
                rect.height,
              ].map((v) => Math.round(v * 100) / 100),
              icon: [icon.x - rect.x, icon.y - rect.y].map(
                (v) => Math.round(v * 100) / 100,
              ),
              contained: contents.every(
                (r) =>
                  r.left >= rect.left + 6 &&
                  r.right <= rect.right - 6 &&
                  r.top >= rect.top + 4 &&
                  r.bottom <= rect.bottom - 4,
              ),
            };
          }),
        );
        expect(
          layout.every((item) => item.contained),
          `${width}px / ${fontSize}px text / ${selection}`,
        ).toBeTruthy();
        const boxes = layout.map(({ box, icon }) => ({ box, icon }));
        if (previous)
          expect(
            boxes,
            "Selecting a platform must not move its button or icon",
          ).toEqual(previous);
        previous = boxes;
        const overflow = await page.evaluate(() =>
          [...document.querySelectorAll("body *")]
            .filter((el) => {
              const r = el.getBoundingClientRect();
              return r.width > 0 && (r.right > innerWidth + 1 || r.left < -1);
            })
            .map((el) => `${el.tagName}.${el.className}`),
        );
        expect(overflow, `${width}px / ${fontSize}px text`).toEqual([]);
        if (
          fontSize === 16 &&
          width === 390 &&
          selection.length === 1 &&
          selection[0] === 0
        )
          await capture(page, "platform-selector-390.png");
      }
    }
  }
});

test("continuous duration must be finite and app token is never part of the saved draft", async ({
  page,
}) => {
  await connect(page);
  await page
    .getByRole("button", { name: "Continuous sending", exact: true })
    .click();
  await page.getByLabel("Run for", { exact: true }).selectOption("custom");
  await page.getByLabel("Duration (minutes)", { exact: true }).fill("0");
  await page
    .getByRole("button", { name: "Generate preview", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText("Choose a duration");
  await expect(
    page.getByRole("heading", { name: "Customize traffic" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Back", exact: true }).click();
  await page
    .getByRole("button", {
      name: "Sessions + distributed traces",
      exact: false,
    })
    .click();
  await page
    .getByLabel("Chronosphere API token", { exact: true })
    .fill("never-submit-this-local-value");
  const stored = await page.evaluate(() => JSON.stringify({ ...localStorage }));
  expect(stored).not.toContain("never-submit-this-local-value");
  await page.reload();
  await expect(
    page.getByLabel("Chronosphere API token", { exact: true }),
  ).toHaveValue("");
});

test("web apps accept a Symbol Upload token that is saved server-side and enables source maps", async ({
  page,
}) => {
  let configured = false;
  const saved: string[] = [];
  await page.route("**/api/settings", (r) =>
    r.fulfill({
      json: {
        secrets: { chronosphere: false, symbol_upload: configured },
        defaults: {},
      },
    }),
  );
  await page.route("**/api/settings/secrets/symbol_upload", (r) => {
    expect(r.request().method()).toBe("PUT");
    saved.push(r.request().postDataJSON().value);
    configured = true;
    return r.fulfill({ json: { name: "symbol_upload", configured: true } });
  });
  await page.goto("/");
  await expect(
    page.getByLabel("Symbol Upload token", { exact: true }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "Web", exact: true }).click();
  await page.getByRole("button", { name: "Android", exact: true }).click();
  await page.getByLabel("Web app ID", { exact: true }).fill("kfs9f");
  await page
    .getByRole("button", { name: "Sessions only", exact: false })
    .click();
  const settingsLink = page.getByRole("link", { name: /Settings → API/ });
  await expect(settingsLink).toHaveAttribute(
    "href",
    "https://dash.embrace.io/settings/organization/api",
  );
  await expect(settingsLink).toHaveAttribute("target", "_blank");
  await expect(
    page.getByRole("link", { name: /Source map upload guide/ }),
  ).toHaveAttribute(
    "href",
    "https://embrace.io/docs/web/getting-started/sourcemap-uploads/",
  );
  await page
    .getByLabel("Symbol Upload token", { exact: true })
    .fill("local-symbol-value");
  const stored = await page.evaluate(() => JSON.stringify({ ...localStorage }));
  expect(stored).not.toContain("local-symbol-value");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Customize traffic" }),
  ).toBeVisible();
  expect(saved).toEqual(["local-symbol-value"]);
  await page.getByRole("button", { name: "Back", exact: true }).click();
  await expect(
    page.getByLabel("Symbol Upload token", { exact: true }),
  ).toHaveValue("");
  await expect(
    page.getByText(/^Saved ••••.*Source maps upload before each live run\./),
  ).toBeVisible();
  await expect(page.getByRole("link", { name: /Settings → API/ })).toHaveCount(0);
  await expect(
    page.getByRole("link", { name: /Source map upload guide/ }),
  ).toBeVisible();
});

test("configured source maps upload automatically before live start and failures never start a run", async ({
  page,
}) => {
  let failUpload = true;
  const calls: string[] = [];
  await page.route("**/api/settings", (r) =>
    r.fulfill({
      json: {
        secrets: { chronosphere: false, symbol_upload: true },
        defaults: {},
      },
    }),
  );
  await page.route("**/api/specs/*/symbols/upload", (r) => {
    calls.push("maps");
    expect(r.request().postDataJSON()).toEqual({ confirm: true });
    return failUpload
      ? r.fulfill({
          status: 422,
          json: {
            detail: "Source map upload failed. Check the Symbol Upload token.",
          },
        })
      : r.fulfill({ json: { uploaded: 42, receipts: [] } });
  });
  await page.route("**/api/runs", (r) => {
    calls.push("run");
    // Inspect the ordering without creating any live worker or external traffic.
    return r.fulfill({
      status: 400,
      json: { detail: "Live start intercepted by isolated test." },
    });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Web", exact: true }).click();
  await page.getByRole("button", { name: "Android", exact: true }).click();
  await page.getByLabel("Web app ID", { exact: true }).fill("kfs9f");
  await page
    .getByRole("button", { name: "Sessions only", exact: false })
    .click();
  await expect(
    page.getByRole("heading", { name: "JavaScript stack traces" }),
  ).toHaveCount(0);
  await expect(
    page.getByLabel("Embrace Symbol Upload token", { exact: true }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await preview(page);
  await page
    .getByRole("button", { name: "Send to destination", exact: false })
    .click();
  await page.getByRole("button", { name: "Send batch", exact: true }).click();
  await expect(
    page.getByText("Source map upload failed. Check the Symbol Upload token.", {
      exact: true,
    }),
  ).toBeVisible();
  expect(calls).toEqual(["maps"]);
  expect(
    await page.evaluate(() => localStorage.getItem("sg_launch_attempt")),
  ).toBeNull();
  await expect(
    page.getByText(/A start request has an unconfirmed/),
  ).toHaveCount(0);
  failUpload = false;
  await page.getByRole("button", { name: "Send batch", exact: true }).click();
  await expect(
    page.getByText("Live start intercepted by isolated test.", { exact: true }),
  ).toBeVisible();
  expect(calls).toEqual(["maps", "maps", "run"]);
});

test("web sending needs no stack settings or upload token when maps are not configured", async ({
  page,
}) => {
  let uploads = 0;
  await page.route("**/api/settings", (r) =>
    r.fulfill({
      json: {
        secrets: { chronosphere: false, symbol_upload: false },
        defaults: {},
      },
    }),
  );
  await page.route("**/api/specs/*/symbols/upload", (r) => {
    uploads++;
    return r.fulfill({
      status: 422,
      json: { detail: "Unexpected map upload." },
    });
  });
  await page.route("**/api/runs", (r) =>
    r.fulfill({
      status: 400,
      json: { detail: "Live start intercepted by isolated test." },
    }),
  );
  await connect(page, ["web"]);
  await preview(page);
  await page
    .getByRole("button", { name: "Send to destination", exact: false })
    .click();
  await expect(
    page.getByRole("button", { name: "Send batch", exact: true }),
  ).toBeEnabled();
  await page.getByRole("button", { name: "Send batch", exact: true }).click();
  await expect(
    page.getByText("Live start intercepted by isolated test.", { exact: true }),
  ).toBeVisible();
  expect(uploads).toBe(0);
});

test("causal scenarios keep platform evidence and run through the workflow", async ({ page }, testInfo) => {
  await connect(page, ["android", "ios", "web"]);
  await page.getByRole("button", { name: "Back", exact: true }).click();
  await page.getByRole("button", { name: "Sessions + distributed traces", exact: false }).click();
  await page.getByLabel("Chronosphere tenant", { exact: true }).fill("preview-tenant");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await pick(page, "Diagnostic story");
  await pick(page, "Slow transaction history");
  await pick(page, "Affected");
  await expect(page.getByText("Every session shows the failure.")).toBeVisible();
  await advanced(page, "Story");
  await expect(page.getByLabel("Traffic mix", { exact: true })).toHaveValue("single");
  await page.getByRole("button", { name: "Done", exact: true }).click();
  await page.getByLabel("Number of sessions").fill("9");
  await page.screenshot({path:testInfo.outputPath("causal-controls.png"),fullPage:true});
  await page.getByRole("button", { name: "Generate preview", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Scenario evidence" })).toBeVisible({ timeout: 45000 });
  await expect(page.getByText("history.query", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Download evidence" })).toBeVisible();
  await expect(page.getByText("Payloads validated · destination unverified")).toBeVisible();
  await expect(page.locator(".preview-stats > div").filter({hasText:"Sessions with traces"})).toContainText("9");
  for (const name of ["Android", "iOS", "Web"]) {
    await page.getByRole("group", {name:"Preview platform"}).getByRole("button", {name,exact:true}).click();
    await expect(page.locator(".waterfall-row").first()).toBeVisible();
    await expect(page.getByText("history.query",{exact:true})).toBeVisible();
  }
  await page.screenshot({path:testInfo.outputPath("causal-preview.png"),fullPage:true});
  await page.getByRole("button", { name: "Continue to send", exact: true }).click();
  await expect(page.getByText("Slow transaction history", { exact: true })).toBeVisible();
  await expect(page.getByText("affected · single · seed 7331")).toBeVisible();
});

test('preset-led performance editor preserves advanced values and previews emitted evidence', async ({page}, testInfo)=>{
 await connect(page,['web']);
 await advanced(page,'Speed');
 await pick(page,'Tail-only slowdown');
 await page.locator('.sheet-panel > details.tab-disclosure > summary').click();
 await expect(page.getByLabel('How much slower',{exact:true})).toHaveValue('4');
 await expect(page.getByText(/Most requests take about \d+ ms; 1 in 20 take over/)).toBeVisible();
 await expect(page.getByRole('img',{name:'Distribution of baseline and affected durations'})).toBeVisible();
 await page.locator('.distribution-editor > details > summary').click();
 await expect(page.getByLabel('Distribution family',{exact:true}).locator('option')).toHaveCount(8);
 await page.getByLabel('Distribution family',{exact:true}).selectOption('bimodal');
 await page.getByLabel('Second mean',{exact:true}).fill('720');
 await expect(page.getByRole('img',{name:'Distribution of baseline and affected durations'})).toBeVisible();
 await capture(page,'v3-distribution-editor.png');
 const download=page.waitForEvent('download');
 await page.getByRole('button',{name:'Save configuration',exact:true}).click();
 const saved=await (await download).path();
 await page.getByRole('button',{name:'Done',exact:true}).click();
 await expect(page.getByText(/Also: Backend processing time change/)).toBeVisible();
 await capture(page,'v3-customize.png');
 await page.getByRole('button',{name:'Back',exact:true}).click();
 await page.getByRole('button',{name:'Continue',exact:true}).click();
 await advanced(page,'Speed');
 await page.locator('.sheet-panel > details.tab-disclosure > summary').click();
 await page.locator('.distribution-editor > details > summary').click();
 await expect(page.getByLabel('Second mean',{exact:true})).toHaveValue('720');
 await page.getByLabel('Second mean',{exact:true}).fill('840');
 await page.locator('input[type=file]').setInputFiles(saved!);
 await expect(page.getByText('Configuration opened.',{exact:true})).toBeVisible();
 await expect(page.getByLabel('Second mean',{exact:true})).toHaveValue('720');
 await page.getByRole('button',{name:'Done',exact:true}).click();
 await page.getByLabel('Number of sessions',{exact:true}).fill('24');
 const response=page.waitForResponse(r=>r.url().endsWith('/api/preview')&&r.request().method()==='POST');
 await page.getByRole('button',{name:'Generate preview',exact:true}).click();
 const data=await (await response).json();
 expect(data.linter).toEqual([]);
 expect(data.report.modeling.effects[0].modeled_after.count).toBeGreaterThan(0);
 await page.getByText('Performance and experiment evidence',{exact:true}).click();
 await expect(page.getByRole('columnheader',{name:'Observed in payloads'})).toBeVisible();
 await capture(page,'v3-preview.png');
 await page.screenshot({path:testInfo.outputPath('v3-preview.png'),fullPage:true});
});

test('advanced modeling controls fit narrow and zoomed viewports',async({page})=>{
 await connect(page,['android','ios','web']);
 const fits=()=>page.evaluate(()=>{
  const sheet=document.querySelector('.sheet-body');
  const wide=[...document.querySelectorAll('.sheet *, main *')].filter(el=>{const r=el.getBoundingClientRect();return r.width>0&&(r.right>innerWidth+1||r.left<-1)&&!el.closest('.sheet-tabs')});
  return document.documentElement.scrollWidth<=innerWidth+1&&(!sheet||sheet.scrollWidth<=sheet.clientWidth+1)&&wide.length===0;
 });
 for(const width of [320,390,768,1280,1920]){
  await page.setViewportSize({width,height:1000});
  expect(await fits(),`customize ${width}px`).toBeTruthy();
 }
 await advanced(page,'Errors');
 await page.getByRole('group',{name:'Network failures pattern'}).getByRole('button',{name:'Spike',exact:true}).click();
 for(const width of [320,390,768,1280,1920]){
  await page.setViewportSize({width,height:1000});
  expect(await fits(),`errors ${width}px`).toBeTruthy();
 }
 await page.getByRole('tab',{name:'Speed',exact:true}).click();
 await page.locator('.sheet-panel > details.tab-disclosure > summary').click();
 await page.getByLabel('Add a speed change for',{exact:true}).selectOption('network_rtt_ms');
 await page.locator('.distribution-editor > details > summary').click();
 for(const width of [320,390,768,1280,1920]){
  await page.setViewportSize({width,height:1000});
  expect(await fits(),`performance ${width}px`).toBeTruthy();
 }
 await page.setViewportSize({width:768,height:1000});
 await page.addStyleTag({content:'html{font-size:200%}'});
 for(const tab of ['Speed','Errors','Visitors','User journeys','A/B tests','App versions']){
  await page.getByRole('tab',{name:tab,exact:true}).click();
  expect(await fits(),`${tab} at 200% text`).toBeTruthy();
 }
 await page.keyboard.press('Escape');
 expect(await fits(),'customize at 200% text').toBeTruthy();
});

test('advanced tabs offer named quick choices, 100% sliders and friendly actions',async({page})=>{
 await connect(page,['web']);
 await advanced(page,'User journeys');
 await pick(page,'Mostly buying');
 await page.locator('.sheet-panel > details.tab-disclosure > summary').click();
 // Moving one share rebalances the rest so the total stays 100%.
 await setRange(page,'Buys',60);
 const values=await page.locator('fieldset.share-sliders').first().locator('input[type=range]').evaluateAll(els=>els.map(e=>Number((e as HTMLInputElement).value)));
 expect(values.reduce((a,b)=>a+b,0)).toBe(100);
 expect(values[1]).toBe(60);
 await expect(page.getByRole('radio',{name:/Mostly buying/})).not.toBeChecked();
 await page.getByRole('tab',{name:'Speed',exact:true}).click();
 await pick(page,'Slow network');
 await page.getByRole('tab',{name:'A/B tests',exact:true}).click();
 await page.getByRole('button',{name:'Add an A/B test',exact:true}).click();
 await page.getByLabel('Test name',{exact:true}).fill('Checkout Redesign');
 await page.getByLabel('Test name',{exact:true}).blur();
 await expect(page.getByLabel('Test name',{exact:true})).toHaveValue('checkout-redesign');
 await page.getByLabel('Starts when users reach',{exact:true}).selectOption({label:'Adds to cart'});
 await setRange(page,'Split',30);
 await page.getByRole('button',{name:'Done',exact:true}).click();
 const response=page.waitForResponse(r=>r.url().endsWith('/api/preview')&&r.request().method()==='POST');
 await page.getByRole('button',{name:'Generate preview',exact:true}).click();
 const sent=(await response).request().postDataJSON();
 expect(sent.population.intent_mix.retail.buyer).toBe(60);
 expect(Object.values(sent.population.intent_mix.retail as Record<string,number>).reduce((a,b)=>a+b,0)).toBe(100);
 expect(sent.modeling.bindings).toMatchObject([{id:'scenario-network',quantity:'network_rtt_ms'}]);
 expect(sent.modeling.experiments).toMatchObject([{id:'checkout-redesign',exposure_endpoints:['add_to_cart'],variants:{control:30,treatment:70}}]);
});

test('release adoption and experiment editors survive reload and reach payload evidence',async({page})=>{
 await connect(page,['android','ios','web']);
 await advanced(page,'App versions');
 await pick(page,'New version rolls out');
 await page.locator('.sheet-panel > details.tab-disclosure > summary').click();
 const android=page.locator('.release-card').filter({hasText:'Android'});
 for(const label of ['Share at the start','Share at the end'])
  await android.getByLabel(label,{exact:true}).evaluate((el:HTMLInputElement)=>{Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value')!.set!.call(el,'100');el.dispatchEvent(new Event('input',{bubbles:true}));});
 await expect(android.getByRole('img',{name:/10\.28\.0 share from 100% to 100%/})).toBeVisible();
 for(const width of [320,768,1440]){
  await page.setViewportSize({width,height:1000});
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1)).toBeTruthy();
 }
 await page.getByRole('tab',{name:'A/B tests',exact:true}).click();
 await page.getByRole('button',{name:'Add an A/B test',exact:true}).click();
 await page.getByText('Expert',{exact:true}).click();
 await page.getByLabel('Starts at any of',{exact:true}).fill('home, accounts, locations');
 await page.reload();
 await page.getByRole('button',{name:'Continue',exact:true}).click();
 await advanced(page,'App versions');
 await expect(page.locator('.release-card').filter({hasText:'Android'}).getByLabel('Share at the start',{exact:true})).toHaveValue('100');
 await page.getByRole('button',{name:'Done',exact:true}).click();
 const response=page.waitForResponse(r=>r.url().endsWith('/api/preview')&&r.request().method()==='POST');
 await page.getByRole('button',{name:'Generate preview',exact:true}).click();
 const data=await(await response).json();
 expect(data.report.modeling.releases[0].observed).toEqual([{version:'10.28.0',visits:data.report.modeling.releases[0].visits,percent:100}]);
 expect(data.report.modeling.experiments[0].variants.reduce((n:number,v:any)=>n+v.exposed_visits,0)).toBeGreaterThan(0);
});

test('continuous runs show achieved rate and an actionable capacity notice', async ({page}) => {
  await page.route('**/api/runs/capacity-run', r => r.fulfill({json: {
    id:'capacity-run', spec_id:'capacity-spec', spec_version:1, mode:'live', status:'running',
    desired_status:'running', started:Date.now()/1000-90, heartbeat:Date.now()/1000, dry_run:true,
    counters:{sessions:210,payloads:1000,accepted:1000,rejected:0,errors:0}, delivery:{}, receipts:[],
    report:{}, verifications:[], achieved_rate_per_minute:140, capacity_limited:true, capacity_reason:'sender_saturation'
  }}));
  await page.route('**/api/specs/capacity-spec*', r => r.fulfill({json:{id:'capacity-spec',version:1,spec:{
    name:'Vantra traffic', targets:[], destinations:{mode:'sessions_only'},
    traffic:{mode:'live',rate_per_minute:600,duration_minutes:120}
  }}}));
  await page.addInitScript(() => localStorage.setItem('sg_run_id', JSON.stringify('capacity-run')));
  await page.goto('/');
  const schedule=page.getByRole('region',{name:'Sending schedule'});
  await expect(schedule).toContainText('600 sessions/min requested');
  await expect(schedule).toContainText('140 sessions/min achieved');
  await expect(schedule.getByRole('status')).toContainText('Generating about 140 of the requested 600 sessions per minute.');
  await expect(schedule.getByRole('status')).toContainText('check the destination connection');
});
