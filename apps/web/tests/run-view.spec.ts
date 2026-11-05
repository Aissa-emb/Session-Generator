import { test, expect, Page } from "@playwright/test";
import { mkdir } from "node:fs/promises";
import path from "node:path";

// Isolated API fixtures exercise presentation of states that must not be
// manufactured by altering the owner's active telemetry run.
const id = "0123456789abcdef0123456789abcdef";
async function fixture(page: Page) {
  let state: any = {
    id,
    spec_id: "visual-spec",
    spec_version: 1,
    status: "running",
    desired_status: "running",
    mode: "live",
    dry_run: false,
    started: Date.now() / 1000 - 724,
    ended: null,
    counters: { sessions: 134, accepted: 24096, errors: 0 },
    delivery: {
      sessions_acknowledged: 128,
      backend_spans_acknowledged: 8924,
      sessions_stored: null,
      backend_spans_stored: null,
    },
    receipts: Array.from({ length: 8 }, (_, n) => ({
      id: `receipt-${n}`,
      time_ms: Date.now() - n * 3000,
      target: n % 2 ? "otlp" : "embrace",
      kind: n % 2 ? "traces" : "logs",
      status: "accepted",
      http_status: 200,
      accepted: n % 2 ? 23 : 8,
      rejected: 0,
    })),
  };
  const spec: any = {
    name: "Vantra Web",
    targets: [
      {
        id: "web",
        platform: "web",
        app_name: "Vantra Web",
        embrace_app_id: "kfs9f",
      },
    ],
    destinations: { mode: "end_to_end" },
    chronosphere: { tenant: "demo-sandbox" },
    traffic: { rate_per_minute: 10, duration_minutes: 20 },
  };
  let offline = false,
    missing = false;
  await page.addInitScript(
    (id) => localStorage.setItem("sg_run_id", JSON.stringify(id)),
    id,
  );
  await page.route("**/api/runs/" + id, (r) =>
    offline
      ? r.abort()
      : missing
        ? r.fulfill({ status: 404, json: { detail: "Run not found." } })
        : r.fulfill({
            json: { ...state, heartbeat: state.heartbeat ?? Date.now() / 1000 },
          }),
  );
  await page.route("**/api/specs/visual-spec?*", (r) =>
    r.fulfill({ json: { spec } }),
  );
  await page.goto("/");
  await expect(page.locator(".run-status")).toHaveText("Running");
  await expect(
    page.getByRole("heading", { name: "Vantra Web", exact: true }),
  ).toBeVisible();
  return {
    state,
    spec,
    offline: () => {
      offline = true;
    },
    online: () => {
      offline = false;
    },
    missing: () => {
      missing = true;
    },
  };
}
async function capture(page: Page, name: string) {
  if (!process.env.QA_SCREENSHOTS_DIR) return;
  await mkdir(process.env.QA_SCREENSHOTS_DIR, { recursive: true });
  await page.screenshot({
    path: path.join(process.env.QA_SCREENSHOTS_DIR, name),
    fullPage: false,
  });
}

for (const reducedMotion of ["no-preference", "reduce"] as const) {
  test(`run controls crossfade safely and keep their footprint (${reducedMotion})`, async ({page}) => {
    await page.emulateMedia({reducedMotion});
    await page.addInitScript(() => {
      (window as any).stageEntrances = 0;
      document.addEventListener('animationstart', e => {
        if ((e as AnimationEvent).animationName === 'stage-enter') (window as any).stageEntrances++;
      });
    });
    const f = await fixture(page);
    await page.setViewportSize({width:390,height:844});
    const actions = page.locator('.run-actions');
    const before = await actions.boundingBox();
    const entrances = await page.evaluate(()=>(window as any).stageEntrances);
    // Polling updates data without animating the entire stage again.
    f.state.delivery.sessions_acknowledged = 129;
    await expect(page.locator('.run-metrics dd').first()).toHaveText('129');
    expect(await page.evaluate(()=>(window as any).stageEntrances)).toBe(entrances);
    let stops = 0;
    await page.route('**/api/runs/' + id + '/stop', async route => {
      stops++;
      f.state.status = 'stopped';
      f.state.desired_status = 'stopped';
      f.state.ended = Date.now()/1000;
      await route.fulfill({json:f.state});
    });
    await page.getByRole('button',{name:'Stop',exact:true}).click();
    await expect(page.getByRole('button',{name:'Create another run',exact:true})).toBeEnabled();
    await expect(page.getByRole('button',{name:'Create another run',exact:true})).toBeFocused();
    expect(stops).toBe(1);
    await expect(page.getByRole('button',{name:'Stop',exact:true})).toHaveCount(0);
    // Outgoing visuals remain briefly, but cannot be focused or invoked.
    const outgoing = actions.locator('.run-actions-layer').first();
    await expect(outgoing).toHaveAttribute('inert', '');
    await expect(outgoing.locator('button').last()).toBeDisabled();
    await outgoing.locator('button').last().evaluate((el: HTMLButtonElement)=>el.focus());
    await expect(page.getByRole('button',{name:'Create another run',exact:true})).toBeFocused();
    const after = await actions.boundingBox();
    expect(Math.abs(after!.width - before!.width)).toBeLessThan(1);
    expect(Math.abs(after!.height - before!.height)).toBeLessThan(1);
    const duration = await outgoing.evaluate(el => getComputedStyle(el).transitionDuration);
    expect(duration.split(',').every(v => parseFloat(v) === 0)).toBe(reducedMotion === 'reduce');
    await page.getByRole('button',{name:'Create another run',exact:true}).click();
    await expect(page.getByRole('heading',{name:'Choose a destination'})).toBeVisible();
    const animation = await page.locator('.workflow-stage').evaluate(el=>getComputedStyle(el).animationName);
    expect(animation).toBe(reducedMotion === 'reduce' ? 'none' : 'stage-enter');
    expect(await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
}

test("run lifecycle, freshness, missing values and counter units stay truthful", async ({
  page,
}) => {
  const f = await fixture(page);
  await expect(page.locator(".run-metrics")).toContainText(
    "Sessions acknowledged",
  );
  await expect(page.locator(".run-metrics")).toContainText("8,924");
  await expect(
    page.getByRole("img", { name: "Live delivery activity", exact: true }),
  ).toBeVisible();
  await expect(page.locator(".activity-mark")).toHaveCount(8);
  await expect(page.locator(".run-metrics")).toContainText(
    "Backend spans acknowledged",
  );
  await expect(page.locator(".run-metrics")).toContainText("128");
  await expect(page.locator(".run-metrics")).toContainText(
    "Stored · Not verified",
  );
  await expect(page.locator(".run-destination")).toContainText("XCOR RUM");
  await expect(
    page.getByRole("cell", { name: "Embrace", exact: true }),
  ).toHaveCount(0);
  f.offline();
  await expect(page.locator(".run-status")).toHaveText("Last known: Running", {
    timeout: 12000,
  });
  await expect(page.locator(".run-metrics")).toContainText("8,924");
  await expect(page.getByText(/Last received/)).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Pause", exact: true }),
  ).toBeDisabled();
  await expect(page.locator(".delivery-activity.is-active")).toHaveCount(0);
  await capture(page, "redesign-disconnected-fixture.png");
  f.online();
  f.state.heartbeat = Date.now() / 1000 - 60;
  await expect(page.getByText("Worker updates are delayed.")).toBeVisible();
  delete f.state.heartbeat;
  f.state.desired_status = "paused";
  await expect(page.locator(".run-status")).toHaveText("Pausing");
  await expect(
    page.getByText("Finishing in-flight sessions before pausing."),
  ).toBeVisible();
  f.state.status = "paused";
  await expect(page.locator(".run-status")).toHaveText("Paused");
  await expect(
    page.getByRole("button", { name: "Resume", exact: true }),
  ).toBeEnabled();
  await expect(page.locator(".delivery-activity.is-active")).toHaveCount(0);
  await capture(page, "redesign-paused-fixture.png");
  f.state.status = "running";
  f.state.desired_status = "stopped";
  await expect(page.locator(".run-status")).toHaveText("Stopping");
  f.state.status = "completed";
  f.state.ended = Date.now() / 1000;
  await expect(page.locator(".run-status")).toHaveText("Completed");
  await expect(
    page.getByRole("button", { name: "Stop", exact: true }),
  ).toHaveCount(0);
  await capture(page, "redesign-completed-fixture.png");
  f.state.status = "failed";
  f.state.counters.errors = 2;
  f.state.receipts[0] = {
    ...f.state.receipts[0],
    status: "rejected",
    http_status: 429,
    accepted: 0,
    rejected: 8,
  };
  await expect(page.locator(".run-status")).toHaveText("Needs attention");
  await expect(
    page.getByRole("cell", { name: "429", exact: true }),
  ).toBeVisible();
  await capture(page, "redesign-error-fixture.png");
  f.state.mode = "burst";
  f.state.counters = {
    embrace_spans: 4,
    embrace_logs: 2,
    otlp_traces: 3,
    errors: 0,
  };
  f.state.sessions = 4;
  f.state.delivery = {
    sessions_acknowledged: 4,
    backend_spans_acknowledged: 67,
  };
  await expect(page.locator(".run-metrics dd").first()).toHaveText("4");
  await expect(page.locator(".run-metrics dd").nth(1)).toHaveText("67");
  delete f.state.delivery;
  f.state.counters = {};
  delete f.state.sessions;
  await expect(page.locator(".run-metrics dd").first()).toHaveText("—");
  await expect(page.locator(".run-metrics dd").nth(1)).toHaveText("—");
  await expect(page.locator(".run-metrics dd").nth(2)).toHaveText("—");
  f.state.mode = "live";
  f.spec.traffic.duration_minutes = 0;
  await page.reload();
  await expect(page.getByText("No time limit", { exact: true })).toBeVisible();
  await expect(page.getByRole("progressbar")).toHaveCount(0);
  f.missing();
  await page.reload();
  await expect(page.locator(".run-status")).toHaveText("Run unavailable");
  await expect(
    page.getByRole("button", { name: "Return to setup" }),
  ).toBeVisible();
});

test("run layout exposes receipts, scales text, and supports keyboard inspection", async ({
  page,
  context,
}) => {
  const f = await fixture(page);
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.getByRole("button", { name: "Copy full run ID" }).click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(id);
  const inspector = page.getByRole("button", {
    name: "Inspect delivery receipt receipt-0",
    exact: true,
  });
  await inspector.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("dialog")).toContainText('"http_status": 200');
  await page.keyboard.press("Escape");
  await expect(inspector).toBeFocused();
  for (const size of [
    { width: 1440, height: 900 },
    { width: 1280, height: 800 },
    { width: 390, height: 844 },
  ]) {
    await page.setViewportSize(size);
    await page.evaluate(() => document.fonts.ready);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBeTruthy();
    if (size.width > 1000) {
      const row = await page
        .locator(".receipt-section tbody tr")
        .nth(3)
        .boundingBox();
      expect(row!.y + row!.height).toBeLessThan(size.height);
    }
    await capture(page, `redesign-run-${size.width}-fixture.png`);
  }
  await page.emulateMedia({ reducedMotion: "reduce" });
  expect(
    await page
      .locator(".run-view")
      .evaluate((e) => e.getAnimations({ subtree: true }).length),
  ).toBe(0);
  f.spec.name =
    "Vantra international customer experience across all platforms and regions";
  f.spec.targets[0].app_name =
    "Vantra Web customer portal with an exceptionally long application name";
  f.spec.chronosphere.tenant =
    "international-customer-experience-tenant-with-a-long-name";
  f.state.delivery.backend_spans_acknowledged = 9876543210;
  await page.reload();
  await expect(page.locator(".run-metrics")).toContainText("9,876,543,210");
  await page.addStyleTag({ content: "html { font-size: 200%; }" });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
  await expect(
    page.getByRole("button", { name: "Stop", exact: true }),
  ).toBeVisible();
  await capture(page, "redesign-large-text-fixture.png");
});

test("receipt filters reflect recorded outcomes and new acknowledgements animate once", async ({
  page,
}) => {
  const f = await fixture(page);
  const outcome = page.getByRole("combobox", { name: "Receipt outcome" });
  await outcome.selectOption("attention");
  await expect(page.getByText("No receipts match this filter")).toBeVisible();
  f.state.receipts.unshift({
    id: "receipt-rejected",
    time_ms: Date.now(),
    target: "otlp",
    kind: "traces",
    status: "rejected",
    http_status: 429,
    accepted: 0,
    rejected: 4,
  });
  await expect(page.locator(".receipt-section tbody tr")).toHaveCount(1);
  await expect(page.locator(".activity-mark.problem")).toHaveCount(1);
  await expect(page.locator(".receipt-section tbody")).toContainText("429");
  await outcome.selectOption("accepted");
  await expect(page.locator(".receipt-section tbody tr")).toHaveCount(8);
  await outcome.selectOption("all");
  await expect(page.locator(".receipt-section tbody tr")).toHaveCount(9);
  // A UI clock tick must not replay the acknowledgement feedback.
  await expect
    .poll(() =>
      page
        .locator(".receipt-fresh")
        .evaluate((el) => el.getAnimations().length),
    )
    .toBe(0);
  f.state.desired_status = "paused";
  await expect(page.locator(".run-status")).toHaveText("Pausing");
  await expect(page.locator(".receipt-fresh")).toHaveCount(0);
  expect(
    await page
      .locator(".delivery-activity")
      .evaluate((el) => el.getAnimations({ subtree: true }).length),
  ).toBe(0);
});
