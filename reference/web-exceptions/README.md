# Executable JavaScript exception corpus

The generator uses real error stacks captured from compiled application modules, with source maps for their exact generated line/column positions. The corpus is stored in `xcorgen/data/web-exceptions.json`; the runtime needs only Python's standard library.

Rebuild from the repository root:

```sh
npm --prefix apps/web ci
cd apps/web
npx playwright install chromium firefox webkit
npm run exceptions:build
```

The build bundles the storefront, travel and money modules using esbuild with minification and preserved function names. Raw stacks retain meaningful names such as `readPrice` and `updateFlightResults`, while file/line/column positions point to the actual compiled code. A local intercepted browser page triggers nine error families in Chromium, WebKit and Firefox. Every application frame is resolved with an independent source-map consumer before it is stored. The recorded browser versions identify the capture environment; these captures model the browser engines, not every historical browser version.

The capture performs no external requests or telemetry delivery. Failed fetches and imports are intercepted; invalid JSON and storage quota errors are actually raised by the browser. Harness frames are omitted. Native fetch/import errors that lack application frames remain stackless rather than inventing callers. The hydration family uses an application renderer mismatch, not a fabricated React-internal frame.

At runtime, release metadata and Embrace's bundle-ID injection are appended after the compiled code. Generated positions stay unchanged. Each URL/release has a matching bundle ID, JS bundle, map and embedded original source. `tests/test_symbols_regions.py` checks every captured frame with a separate Python VLQ decoder and checks `sourcesContent` against the source files.

Exception generation is automatic, with no stack-trace panel or switch in the workflow. When the server has a symbol-upload credential (entered in the Web app fields of step 1, stored through the settings API, or supplied through `SG_SYMBOL_UPLOAD_TOKEN` / `SG_SYMBOL_UPLOAD_TOKEN_FILE`), the UI enables symbolication and uploads release assets before requesting a live run. Upload failure prevents that symbolicated run; acknowledged uploads are reusable. Without a credential, runs send the actual captured stacks with readable function names and compiled-file positions. Resolving original source files and lines in Embrace still requires uploaded maps. Local tests never upload assets. Existing stored exceptions are not rewritten by the generator.
