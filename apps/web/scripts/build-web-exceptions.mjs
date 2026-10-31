/** Build real JavaScript bundles and record errors raised by all three browser engines.
 * Development tool only. Runtime generation uses the checked-in JSON, with no Node dependency.
 * All browser requests are fulfilled locally or deliberately aborted; no telemetry is sent.
 */
import { build, version as esbuildVersion } from "esbuild";
import { chromium, firefox, webkit } from "@playwright/test";
import { originalPositionFor, TraceMap } from "@jridgewell/trace-mapping";
import { readFile, writeFile, readdir } from "node:fs/promises";
import { createHash } from "node:crypto";
import path from "node:path";
import { fileURLToPath } from "node:url";
const root = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../..",
);
const sourceRoot = path.join(root, "reference/web-exceptions");
const families = [
  "property_undefined",
  "not_a_function",
  "json_html",
  "failed_to_fetch",
  "abort",
  "chunk_load",
  "webpack_chunk_load",
  "quota_exceeded",
  "hydration",
];
const result = {
  compiler: `esbuild ${esbuildVersion}`,
  source_hash: "",
  bundles: {},
};
const hash = createHash("sha256");
async function hashSources(dir) {
  for (const file of (await readdir(dir, { withFileTypes: true })).sort(
    (a, b) => a.name.localeCompare(b.name),
  )) {
    const full = path.join(dir, file.name);
    if (file.isDirectory()) await hashSources(full);
    else
      hash.update(path.relative(sourceRoot, full)).update(await readFile(full));
  }
}
await hashSources(path.join(sourceRoot, "src"));
result.source_hash = hash.digest("hex");
function frames(stack, url) {
  return (stack || "").split("\n").flatMap((line) => {
    const at = line.lastIndexOf(url + ":");
    if (at < 0)
      return /at (JSON.parse|Array.map) \(<anonymous>\)/.test(line)
        ? [{ name: line.match(/at (.*?) \(/)[1], native: true }]
        : [];
    const loc = line.slice(at + url.length + 1).match(/^(\d+):(\d+)/);
    if (!loc) throw new Error("Unparsed frame: " + line);
    const prefix = line.slice(0, at).trim();
    const name =
      prefix === "at"
        ? ""
        : prefix.startsWith("at ")
          ? prefix.slice(3).replace(/\s*\($/, "")
          : prefix.replace(/@$/, "");
    return [{ name, line: Number(loc[1]), column: Number(loc[2]) }];
  });
}
for (const domain of ["storefront", "travel", "money"]) {
  const output = await build({
    absWorkingDir: sourceRoot,
    entryPoints: [`src/${domain}/results.js`],
    bundle: true,
    write: false,
    outfile: "bundle.js",
    sourcemap: "external",
    sourcesContent: true,
    minify: true,
    // Keep runtime function names legible even before source-map processing.
    // Frames still come from executing this exact optimized bundle.
    keepNames: true,
    format: "iife",
    globalName: "VantraApplication",
    target: "es2022",
    legalComments: "none",
  });
  const code = output.outputFiles
    .find((f) => f.path.endsWith(".js"))
    .text.trimEnd();
  const map = JSON.parse(
    output.outputFiles.find((f) => f.path.endsWith(".map")).text,
  );
  const entry = { code, map, engines: {} };
  const url = `https://vantra.invalid/assets/${domain}.js`;
  const snippet =
    ';(E=>{try{const _=globalThis,c=(new _.Error).stack;c&&(_[E]=_[E]||{},_[E][c]="00000000000000000000000000000000")}catch(E){}})("_EmbraceFileBundleIDs");';
  for (const [engine, launcher] of Object.entries({
    chromium,
    webkit,
    gecko: firefox,
  })) {
    const browser = await launcher.launch({ headless: true });
    try {
      const page = await browser.newPage();
      await page.route("**/*", async (route) => {
        const u = route.request().url();
        if (u === url)
          return route.fulfill({
            contentType: "application/javascript",
            body:
              code +
              '\n;globalThis.VantraRelease="build";\n// Injected by Embrace Web CLI:\n' +
              snippet +
              "\n",
          });
        if (u === "https://vantra.invalid/")
          return route.fulfill({
            contentType: "text/html",
            body: `<!doctype html><script src="${url}"></script>`,
          });
        if (u.endsWith("/invalid-json"))
          return route.fulfill({
            contentType: "text/html",
            body: "<!DOCTYPE html><title>Service unavailable</title>",
          });
        return route.abort("failed");
      });
      await page.goto("https://vantra.invalid/");
      const injection = await page.evaluate(
        () => Object.keys(globalThis._EmbraceFileBundleIDs)[0],
      );
      const samples = {
        browser_version: browser.version(),
        injection: frames(injection, url),
        exceptions: {},
      };
      for (const family of families) {
        const e = await page.evaluate(async (family) => {
          const remote = [
            "json_html",
            "failed_to_fetch",
            "abort",
            "chunk_load",
            "webpack_chunk_load",
          ].includes(family);
          const state = {
            operation: family,
            remote,
            records:
              family === "property_undefined" ? [undefined] : [{ price: 12 }],
            recent: "x".repeat(12 * 1024 * 1024),
            serverText: "Available",
            clientText: "Sold out",
            endpoint:
              "https://vantra.invalid/" +
              (family === "json_html" ? "invalid-json" : "unreachable"),
            chunk: "https://vantra.invalid/missing.js",
            chunkId: "731",
          };
          try {
            await globalThis.VantraApplication.initializePage(state);
            return null;
          } catch (e) {
            return {
              type: e.constructor.name,
              name: e.name,
              message: e.message,
              stack: e.stack,
            };
          }
        }, family);
        if (!e) throw new Error(`${engine}/${domain}/${family} did not throw`);
        const actualFrames = frames(e.stack, url);
        for (const f of actualFrames.filter((f) => !f.native)) {
          const original = originalPositionFor(new TraceMap(map), {
            line: f.line,
            column: f.column - 1,
          });
          if (!original.source || !original.line)
            throw new Error(`Unmapped ${family} ${JSON.stringify(f)}`);
          f.original = {
            source: original.source,
            line: original.line,
            column: original.column,
            name: original.name,
          };
        }
        samples.exceptions[family] = {
          ...e,
          message: e.message
            .replaceAll("https://vantra.invalid/missing.js", "{chunk}")
            .replaceAll("731", "{chunk_id}"),
          stack: undefined,
          frames: actualFrames,
        };
      }
      entry.engines[engine] = samples;
    } finally {
      await browser.close();
    }
  }
  result.bundles[domain] = entry;
  console.log(
    `${domain}: captured ${families.length} exception families in Chromium, WebKit and Firefox`,
  );
}
await writeFile(
  path.join(root, "xcorgen/data/web-exceptions.json"),
  JSON.stringify(result, null, 2) + "\n",
);
