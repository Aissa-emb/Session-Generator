import { test, expect, Page } from "@playwright/test";

async function connect(page: Page, platforms = ["android"]) {
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Where should the data go?" }),
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
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "What would you like to generate?" }),
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
  await expect(page.getByText(/copilot|MCP connection/i)).toHaveCount(0);
  expect((await request.post("/mcp", { data: {} })).status()).toBe(404);
  expect(
    (await request.post("/api/copilot/turns", { data: {} })).status(),
  ).toBe(404);
  expect(errors).toEqual([]);
});

test("custom mixed-platform faults produce real session and distributed trace previews", async ({
  page,
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
  await page.locator(".waterfall-row").first().click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Continue to send" }).click();
  await expect(
    page.getByRole("button", { name: "Send batch", exact: true }),
  ).toBeDisabled();
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
    page.getByRole("heading", { name: "Completed", exact: true }),
  ).toBeVisible({ timeout: 45000 });
  await expect(page.locator(".run-metrics").first()).toContainText("4");
  await page.getByText("Recent delivery receipts", { exact: true }).click();
  await expect(
    page.getByRole("cell", { name: "accepted", exact: true }).first(),
  ).toBeVisible();
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "Completed", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Create another run" }).click();
  await expect(page.getByLabel("Android app ID", { exact: true })).toHaveValue(
    "5nrc2",
  );
});

test("continuous rate and two-hour duration start a real local worker with pause and stop", async ({
  page,
}) => {
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
      page.getByRole("heading", { name: "Generating locally", exact: true }),
    ).toBeVisible({ timeout: 45000 });
    await expect(page.locator(".sending-indicator")).toHaveClass(/animating/);
    await expect(
      page.getByRole("progressbar", { name: "Sending duration" }),
    ).toHaveAttribute("max", "7200");
    await expect(
      page.locator(".run-metrics>div").first().locator("strong"),
    ).not.toHaveText("0");
    await page.reload();
    await expect(
      page.getByRole("heading", { name: "Generating locally", exact: true }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Pause", exact: true }).click();
    await expect(
      page.getByRole("heading", { name: /Pausing|Paused/ }),
    ).toBeVisible();
    await expect(page.locator(".sending-indicator")).not.toHaveClass(
      /animating/,
    );
    await page.getByRole("button", { name: "Stop", exact: true }).click();
    await expect(
      page.getByRole("heading", { name: "Stopped", exact: true }),
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
        data: { expected_version: 1, patch: [{ op: "replace", path: "/targets/0/embrace_app_id", value: "5nrc3" }] },
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
  await expect(page.getByRole("button", { name: "Local test", exact: false })).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("button", { name: "Retry start request" }).click();
  await expect(
    page.getByRole("heading", { name: "Completed", exact: true }),
  ).toBeVisible({ timeout: 45000 });
  const id = await page.evaluate(() =>
    JSON.parse(localStorage.getItem("sg_run_id") || "null"),
  );
  expect(id).toBe(created);
  const runs = await (await request.get("/api/runs")).json();
  expect(runs.filter((r: any) => r.id === created)).toHaveLength(1);
  await page.getByRole("button", { name: "Create another run" }).click();
  await expect(page.getByLabel("Android app ID", { exact: true })).toHaveValue("5nrc3");
});

test("a definitive credential preflight error unlocks Destination for correction", async ({ page }) => {
  await connect(page);
  await page.getByLabel("Number of sessions").fill("2");
  await preview(page);
  await page.route("**/api/runs", async (route) => {
    if (route.request().method() === "POST")
      await route.fulfill({ status: 422, json: { detail: "The Cortex XCOR token could not be read. Save it again in Destination. No run was started." } });
    else await route.continue();
  });
  await page.getByRole("button", { name: "Start local test", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("No run was started");
  await expect(page.getByRole("button", { name: "Retry start request" })).toHaveCount(0);
  expect(await page.evaluate(() => localStorage.getItem("sg_launch_attempt"))).toBeNull();
  await page.getByRole("button", { name: /Destination Choose your apps and tenant/ }).click();
  await expect(page.getByRole("heading", { name: "Where should the data go?" })).toBeVisible();
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
    page.getByRole("heading", { name: "What would you like to generate?" }),
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
