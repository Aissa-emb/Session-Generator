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
  await expect(
    page.getByRole("checkbox", { name: "Crashes", exact: false }),
  ).toBeDisabled();
  await expect(
    page.getByRole("checkbox", { name: "Slow page loads", exact: false }),
  ).toBeEnabled();
  await page.getByRole("button", { name: "Regression", exact: false }).click();
  await page.getByLabel("Regression to reproduce").selectOption("pages");
  await expect(
    page.getByRole("checkbox", { name: "Slow page loads", exact: false }),
  ).toBeChecked();
  await expect(
    page.getByLabel("Slow page loads affected percentage"),
  ).toHaveValue("30");
  await capture(page, "redesign-customize.png");
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
  await page.getByLabel("User flow").selectOption("retail");
  for (const name of [
    "Crashes",
    "Exceptions",
    "App freezes",
    "Network failures",
    "Slow page loads",
    "Payment failures",
  ])
    await page.getByRole("checkbox", { name, exact: false }).check();
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
  await page
    .getByRole("checkbox", { name: "Exceptions", exact: false })
    .check();
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
  await page.getByRole("button", { name: "Causal demo", exact: false }).click();
  await page.getByLabel("Scenario", { exact: true }).selectOption("slow-history");
  await page.getByLabel("Cohort", { exact: true }).selectOption("affected");
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
  await expect(page.getByText("slow history", { exact: true })).toBeVisible();
  await expect(page.getByText("affected · single · seed 7331")).toBeVisible();
});
