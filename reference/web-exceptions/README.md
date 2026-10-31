# Executable JavaScript exception corpus

The generator uses real error stacks captured from compiled application modules, with source maps for their exact generated line/column positions. The corpus is stored in `xcorgen/data/web-exceptions.json`; the runtime needs only Python's standard library.

Rebuild from the repository root:

```sh
npm --prefix apps/web ci
cd apps/web
npx playwright install chromium firefox webkit
npm run exceptions:build
```

The build bundles the storefront, travel and money modules using esbuild. A local intercepted browser page triggers nine error families in Chromium, WebKit and Firefox. Every application frame is resolved with an independent source-map consumer before it is stored. The recorded browser versions identify the capture environment; these captures model the browser engines, not every historical browser version.

The capture performs no external requests or telemetry delivery. Failed fetches and imports are intercepted; invalid JSON and storage quota errors are actually raised by the browser. Harness frames are omitted. Native fetch/import errors that lack application frames remain stackless rather than inventing callers. The hydration family uses an application renderer mismatch, not a fabricated React-internal frame.

At runtime, release metadata and Embrace's bundle-ID injection are appended after the compiled code. Generated positions stay unchanged. Each URL/release has a matching bundle ID, JS bundle, map and embedded original source. `tests/test_symbols_regions.py` checks every captured frame with a separate Python VLQ decoder and checks `sourcesContent` against the source files.

In the generator, select Web → Resolve original files and lines and enter the Embrace Symbol Upload token in Destination. Live start uploads the release assets before requesting a run. Upload failure prevents starting; acknowledged bundle uploads are reusable. Local tests never upload assets. Existing stored exceptions are not rewritten by the generator.
