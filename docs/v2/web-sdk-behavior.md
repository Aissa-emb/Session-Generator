# Embrace Web SDK 2.31.0: verified behavior for the web generator

Research done on 2026-10-30 for V2 phase 2. Sources: the SDK source at `~/Claudecode/embrace-web-sdk/packages/web-sdk/src` (abbreviated `SRC/`), web-vitals 6.2.3 (the version in the SDK lockfile), the per-engine integration goldens copied to `reference/web-sdk-golden/`, a real 2.29 production session, scrubbed of personal and account data, in `reference/web-sdk-2.29-sample/web_2_29_0_sample.scrubbed.jsonl` (made with `tools/scrub_web_sample.py` from `~/Claudecode/go/tool/internal/schema-stats/testdata/web_2_29_0_sample.json`; never commit the unscrubbed original), the browser capture in `reference/web-sdk-2.31.0-capture/`, the backend URL grouper (`~/Claudecode/go/lib/grouper/`) and the dashboard (`~/Claudecode/dashjs`). File:line citations refer to `SRC/` unless stated. Where this file and `V2-BUILD-PROMPT.md` disagree, this file (the SDK) wins; the disagreements are listed at the end.

## Encoding rules that apply everywhere

- Whole numbers are sent as `intValue`, fractions as `doubleValue` (a JS number serializes by value). Chromium timestamps and durations are fractional milliseconds; Firefox and WebKit report whole milliseconds (their `timeUnixNano` values end in `000000`). Generate per engine.
- An attribute explicitly set to `undefined` still appears with an empty value `{}` (for example `app.surface.name` on a vital when no route path exists). With route templates this does not occur.
- A log with no body serializes `"body": {}`.
- Session, part, page (`app.surface.id`), log record (`log.record.uid`), device and app-instance IDs are 32 uppercase hex characters with no hyphens (`utils/generateUUID.ts:4-15`). Trace and span IDs are lowercase hex (32 and 16).
- Every attribute value on custom (app-supplied) log and exception attributes is a string, cut to 256 characters; keys are cut to 128; at most 50 custom keys (`managers/EmbraceLimitManager/EmbraceLimitManager.ts:94-120`).

## Resource (identical on spans and logs payloads)

Keys (goldens): `telemetry.sdk.name`=`embrace-web-sdk`, `app_version` (the app's configured version; V2 uses the web release), `app_framework`=1 (int), `sdk_version`=`2.31.0`, `telemetry.sdk.version`, `sdk_simple_version`=1 (int), `sdk_platform`=`web`, `telemetry.sdk.language`=`webjs`, `emb.app_instance_id` (per tab, sessionStorage), `user_agent.original`, `screen_resolution`, `viewport_resolution`, `network_effective_type` and `device_memory` (int) **Chromium only**, `webdriver` (bool false), `service.name`=`embrace-web-sdk` (apps may configure `service.name`, `service.version`, `deployment.environment`, as the 2.29 production sample shows).

## Storage (all keys prefixed `${appID}__`, per origin)

localStorage: `embrace_user_session_state` (JSON, `schemaVersion:1`), `embrace_user_session_number`, `embrace_session_part_number`, `embrace_permanent_properties`, `embrace_last_end_user_session_ts`, `embrace_user_id` (the device ID sent as `X-EM-DID`), `embrace_external_user_id` (`user.id`), `embrace_exception_number`. sessionStorage: `embrace_app_instance_id`. Consequence: www.vantra.com and app.vantra.com have separate device IDs, user sessions, counters and exception numbers.

## Session parts (`managers/EmbraceUserSessionManager/EmbraceUserSessionManager.ts`, USM)

- Span `emb-session-part`, scope `embrace-web-sdk-session-parts` (no version). A part starts only when the tab is engaged (visible and focused). One part at a time.
- Start attributes: `emb.user_session_id`, `emb.user_session_previous_id` ('' if none), `emb.user_session_number` (int), `emb.user_session_part_index` (int), `emb.session_part_number` (int), `emb.user_session_start_ts` (epoch ms, fractional → double), `emb.user_session_max_duration_seconds` 43200, `emb.user_session_inactivity_timeout_seconds` 1800, `emb.user_session_foreground_inactivity_timeout_seconds` 1800, `emb.type`=`ux.session_part`, `emb.state`=`foreground`, `emb.session_part_id`, `emb.session_part_start_reason`, `emb.cold_start` (bool); `emb.previous_export_failed.<reason>` only when set.
- End attributes: `emb.properties.<key>` (strings, stamped at end only), `emb.session_part_end_reason`, counters `emb.unhandled_exceptions_count` and `emb.error_log_count` (int, **absent when zero**, counted even when the item was dropped by limits), `emb.app.applied_limit.<type>.<drop|truncate_string|truncate_attributes>.count`, `emb.sdk_startup_duration` (int, ceil ms, on **every** part), `emb.page_load` (bool `document.readyState==='complete'` at part end, **only on the first part of a page lifetime**), `emb.is_final_session_part`=int 1 and `emb.user_session_termination_reason` (`manual|max_duration_reached|inactivity`) on final parts only.
- `emb.cold_start` is true until the first part of the page lifetime ends, whatever the start reason.
- Start reasons: `init` (SDK init on an engaged tab), `foreground` (visibility or focus regained), `web_activity` (keydown, mousedown, mousemove or scroll while engaged with no part; 30 s leading-edge throttle), `web_soft_navigation`, `user_session_rollover` (after manual end or max duration).
- End reasons: `background` (hidden or blur; not final), `web_foreground_inactivity` (30 min without activity; final, termination `inactivity`), `web_soft_navigation` (not final), `user_session_ended` (`endUserSession()` → `manual`, 5 s cooldown; or max duration → `max_duration_reached`; final).
- Expiry: a stored session is expired if `now < startTs`, `now >= maxEndTs` (12 h) or `now >= inactivityDeadlineTs` (30 min after a non-final part end). An expired session is replaced silently: the old session gets no final part; its last part simply ended `background`. The new session's `emb.user_session_previous_id` is the expired ID.
- Counters: `user_session_number` increments per new user session; `session_part_number` increments at every part start and is never reset; `part_index` starts at 0 per session and increments at each part start (first part = 1).
- Page hide/show and bfcache: no pagehide/pageshow session logic. Hide ends the part `background`; show starts it `foreground`. On a bfcache restore the new part is `foreground`, `emb.cold_start=false`, no `emb.page_load`, same `app.surface.id` (heap preserved). `pageshow` resets zero time.
- Multi-page sites: every page load re-initializes the SDK; the user session continues from localStorage; the new page's first part starts `init` with `emb.cold_start=true`; the previous page's last part ended `background` when the page was hidden by the navigation.

## Navigation API rollover (`managers/EmbracePageManager/EmbracePageManager.ts`)

- Feature test: `window.navigation?.addEventListener('currententrychange')` only (no history patching). Browsers without it never roll over: a single-page app keeps one part for the whole visit and only the surface span changes.
- Every URL change rolls over, including query-only and hash-only changes; skipped only when `event.from.url === location.href`.
- Order: zero time set to the event time; the old part ends and the new part starts at one shared boundary (`getNowMillis()` at handler time); then the route is updated. Query/hash-only changes keep route, label and page ID.

## Surfaces (`instrumentations/navigation`, scope `NavigationInstrumentation`/`1.0.0`)

- Span name `route.path ?? route.url` (`(...)` groups stripped); the SDK-derived url is `location.pathname`. Attributes at start: `emb.type`=`ux.surface`, `app.surface.name` (only when a route path is set), `app.surface.id`.
- Ends on a route change to a different URL, at part end (before the part span ends), or on disable. A new surface span opens on every part start **except** `web_soft_navigation` (the router's `setCurrentRoute` opens it).
- Page ID: new only when `setCurrentRoute` gets a different URL. Label: `route.label`, else `setPageLabel`, else `document.title`.
- `browser.url.full` is read at span end (`document.URL`), so an outgoing surface span carries the **new** URL (confirmed in the 2.29 production sample).

## Soft Navigation span (`instrumentations/soft-navigation-performance`)

- Name `Soft Navigation`, scope `SoftNavigationPerformanceInstrumentation`/`1.0.0`, **no** `emb.type`.
- Native (`emb.soft_navigation.source`=`performance_observer`) when `soft-navigation` is a supported entry type: `browser.url.full`=entry.name, `.navigation_id`, `.interaction_id`, `.start_time` (ms from zero time, clamped ≥0, usually 0), `.duration`, `.paint_time` and `.presentation_time` when not null; span from start to start+duration.
- Polyfill (`polyfill`) when `window.navigation` exists and the `event` entry type is supported: Event Timing observer with `durationThreshold:16`; only `click` entries whose `[startTime, startTime+duration]` contains the navigation time; `.start_time` and `.duration` = navTs − clickStart; `.interaction_id` only when non-zero; no navigation_id, paint_time or presentation_time. Keyboard, programmatic and back-button navigations produce no polyfill span.
- Correlation arrays always present (possibly empty): `emb.soft_navigation.span_ids`, `.span_id_types` (`emb.type` at span start, so network spans are `''`), `.log_ids` (`log.record.uid`), `.log_id_types`.
- At most 100 per part.

## Common attributes

- Every span at end: `browser.url.full` (document.URL), `app.surface.name` (when a path exists), `app.surface.id`, `app.surface.label`, `user.id` (when set), `emb.perf_clock_lag_ms` (int, `round(Date.now()-perf.now)`, typically -1, 0 or 1). **Not** the session or part IDs (only the part span has those).
- Every log: `log.record.uid`, `emb.user_session_id`, `emb.user_session_previous_id`, `emb.session_part_id` ('' when none), `browser.url.full` **and** `url.full`, `emb.is_tab_engaged` (bool, logs only), `user.id`, `app.surface.*`, `emb.perf_clock_lag_ms`. `emb.type` defaults to `sys.log`.

## Export and batching

- Spans: queued until any part span ends; then `[partSpan, ...pending]` goes out as one `/v2/spans` payload. At most 1000 spans and 10000 network spans per part.
- Logs: OTel `BatchLogRecordProcessor` with defaults (OTel spec default schedule delay 1000 ms; flush when the document is hidden). V1 used 5000 ms; use 1000.
- Endpoints `https://a-${appID}.data.emb-api.com/v2/spans` and `/v2/logs`. Headers: `Content-Type: application/json`, `X-EM-AID`, `X-EM-DID`, `Content-Encoding: gzip`, `Content-Length`. `keepalive` while in-flight keepalive bytes ≤48 KiB. Timeout 30 s, 5 concurrent exports, up to 5 retries (1 s ×1.5, cap 5 s).
- The SDK's own uploads (`/v2/spans`, `/v2/logs`, `/v2/config`) are recorded as `perf.network_request` spans; the SDK adds no ignore URLs (target setting `sdk_upload_spans`, default true). An upload started while a page unloads never finishes recording, so it does not appear.

## Network spans

- Scopes `@opentelemetry/instrumentation-fetch`/`0.223.0` and `@opentelemetry/instrumentation-xml-http-request`/`0.223.0`. Span name is the method; kind CLIENT; attributes `http.request.method` (and `http.request.method_original` when the app used a lowercase method), `url.full`, `emb.w3c_traceparent` (when propagated), `http.response.status_code`, `server.address`, `server.port`, then common attributes; `emb.type`=`perf.network_request` is added at end when the method is set, the status is a number (0 qualifies) and the URL contains `://`.
- Events: `fetchStart, domainLookupStart, domainLookupEnd, connectStart, [secureConnectionStart], connectEnd, requestStart, responseStart, responseEnd`; cross-origin requests without `Timing-Allow-Origin` carry only `fetchStart` and `responseEnd` (2.29 sample: analytics, LaunchDarkly). XHR spans add `open` and `send` first. WebKit may omit `responseStart` (golden GET).
- Errors: 4xx/5xx get `error.type`=status string and status code 2 (capture). Offline, CORS and aborted requests are status 0 with an error status. Third-party beacons can report status 0 (2.29 sample).
- `emb.w3c_traceparent` is set by the Embrace propagator, which is registered only when network span forwarding is enabled for the device (remote config threshold; default off), and only on requests where the traceparent header is injected (same origin, or URLs in the fetch instrumentation's `propagateTraceHeaderCorsUrls`, which the app configures). V2: present only in end-to-end mode and only for hosts in the target's `propagation_allow_list`.
- `documentLoad` starts inside a `<meta name="traceparent">` context only when a global propagator exists; page loads are never linked to traces in the dashboard.

## Document load (`instrumentations/document-load`, scope `DocumentLoadInstrumentation`/`1.0.0`)

- Collected once, when the navigation entry has `loadEventEnd > 0`. A visitor who leaves before the load event produces **no** documentLoad, documentFetch or resourceFetch spans (and `emb.page_load=false`).
- `documentLoad`: fetchStart → loadEventEnd; `emb.type`=`ux.document_load`, `url.full`, `user_agent.original`; events (zeros skipped) fetchStart, unloadEventStart/End (previous same-origin page), domInteractive, domContentLoadedEventStart/End, domComplete, loadEventStart/End, plus `firstPaint`/`firstContentfulPaint` from paint entries existing at collection time (Firefox golden has `firstContentfulPaint`; Chromium in production has both; WebKit golden has none).
- `documentFetch` (child): fetchStart → responseEnd; `url.full`; all timing events kept even when equal, except `secureConnectionStart` skipped when 0; `browser.navigation_timing.type` raw (`navigate`, `reload`, `back_forward` with underscore, `prerender`); `browser.navigation_timing.not_restored_reasons` (string array, Chromium, only when non-empty).
- `resourceFetch` (children of documentLoad, **in the same payload**): fetchStart → responseEnd, `url.full`=resource URL, events skip zeros.
- Shared resource attributes in order: `http.response_content_length` (encoded), `http.response_content_length_uncompressed` (only when decoded ≠ encoded), `emb.type`=`ux.resource_fetch`, `http.response.delivery_type` (if truthy, e.g. `cache`; Chromium), `http.request.initiator_type`, `http.request.render_blocking_status` (if truthy; Chromium only), `http.response.status_code` (if truthy; Chromium and Firefox, never WebKit), `http.response.size` (transferSize), `http.response.body.size` (encoded), `http.response.decoded_body_size`.
- Flags, mutually exclusive: `http.response.cors_opaque` (all sizes 0, fetchStart>0, responseEnd>0 — cross-origin without TAO), else `http.request.incomplete` (sizes 0, responseEnd 0), else `http.request.prevented` (sizes 0, fetchStart 0). `http.response.cache_revalidated` independently when transferSize === 300 and deliveryType ≠ `cache`.

## Web vitals (`instrumentations/web-vitals`, scope `WebVitalsInstrumentation`/`1.0.0`)

- Log `eventName`=`browser.web_vital`, severity 9, no severity text, `emb.type`=`ux.web_vital`. Always: `browser.web_vital.name` (lowercase), `.value`, `.delta`, `.rating`, `.id` (`v6-{Date.now()}-{13-digit int}`), `.navigation_type`, `.navigation_id` (int; **0 on Firefox and WebKit**). Conditional: `.interaction_id` (soft navigations), `browser.navigation_timing.not_restored_reasons`, page attribution (`browser.url.full`, `app.surface.*`; soft-nav metrics use the navigation URL).
- `navigation_type` values: `navigate`, `reload`, `back-forward` (entry type with `_`→`-`), `back-forward-cache`, `prerender`, `restore`, `soft-navigation`.
- Reporting: default final reporting (no reportAllChanges); each metric reported once per navigation; listeners once per page. Not added as span events; send each vital once, as a log (the backend accepts span events too and does not deduplicate).
- Timestamp: INP at `timeOrigin + interactionTime`; CLS at the earliest shift (fallback largestShiftTime); TTFB at zero time; LCP and FCP at the last entry's startTime. `emb.session_part_id` = the latest part whose start ≤ that timestamp (earliest part if before all).
- Body = JSON of primitive attribution fields only, in this order: CLS `largestShiftTarget, largestShiftTime, largestShiftValue, loadState` (`{}` with no sources); FCP `timeToFirstByte, firstByteToFCP, loadState`; INP `interactionTarget, interactionType, interactionTime, nextPaintTime, inputDelay, processingDuration, presentationDelay, loadState` plus `totalScriptDuration, totalStyleAndLayoutDuration, totalPaintDuration, totalUnattributedDuration` only with LoAF data; LCP `timeToFirstByte, resourceLoadDelay, resourceLoadDuration, elementRenderDelay, target, url` (url only for resources); TTFB `waitingDuration, cacheDuration, dnsDuration, connectionDuration, requestDuration`.
- Attributes, prefix `emb.web_vital.attribution.`: CLS `clsLayoutShifts` (JSON array of the first 50 `{selector,startRect:{x,y,width,height},endRect:{...}}`, ints) and `clsLayoutShiftsDroppedCount` (only >50); INP `loaf_scripts` (JSON `{"<url or (inline)>":{"total_duration","style_and_layout_duration","count"}}`, sorted by total desc, top 250, only with LoAF) and `element_type` (snake_case, lowercased tag); LCP `elementType` (camelCase) and `elementBoundingRect` (JSON rect, ints); TTFB `redirect, domainLookup, tcpConnection, tlsNegotiation, serverResponse, unattributed` (ints, rounded, ≥0; the source computes their total from responseEnd − startTime, so they can exceed the TTFB metric (responseStart − startTime) by the response download duration).
- Thresholds [good, poor] (web-vitals and `dashjs/src/helpers/webVital.ts`): LCP 2500/4000 ms, FCP 1800/3000, TTFB 800/1800, INP 200/500, CLS 0.1/0.25, TBD 200/600. The backend stores the rating as sent; the dashboard colors p75 itself.
- The backend prefixes most attribution keys with the vital name (`target` → `LCPTarget`) and keeps `interactionTarget`, `largestShiftTarget` and `element` verbatim (`go/lib/codec/web_vital_mapper.go:115-161`).
- Soft-navigation vitals: CLS, FCP, INP and LCP (never TTFB), only when `soft-navigation` is supported and `PerformanceSoftNavigation.prototype.getLargestInteractionContentfulPaint` exists (native soft navigations).

## Long animation frames (`instrumentations/loaf`, scope `LoafInstrumentation`/`1.0.0`)

- Accumulated across `long-animation-frame` entries: count, total duration, work duration (`renderStart-startTime`, else duration), style-and-layout duration, longest duration; the page's very first frame is excluded from `longest_duration_excluding_first` and from blocking.
- TBD = `round(Σ blockingDuration)` over non-first frames with `firstUIEventTimestamp === 0` (not tied to a UI event); blockingDuration comes from the browser.
- At each part end with ≥1 frame, two logs (no timestamp, emit time; part ID of the ending part): `emb-loaf-report` (`emb.type`=`ux.web_vital`, `browser.web_vital.id`=`embracev2-{Date.now()}-{13-digit int}`, `.name`=`tbd`, `.value` int, `.rating`, `emb.tbd.loaf_total_duration`, `_work_duration`, `_style_and_layout_duration`, `_count`, `_longest_duration`, `_longest_duration_excluding_first`, all ints; no delta, no navigation fields, body `{}`) and `emb-loaf-scripts` (`emb.type`=`ux.loaf_scripts`, body JSON of per-script totals keyed by `sourceURL` or `(inline)`; only when scripts were seen). `emb.was_from_inp` does not exist.

## User, element and server timing

- User timing (scope `UserTimingInstrumentation`/`1.0.0`): a **mark** becomes log `emb-user-timing` (INFO, timestamp = mark time) with `emb.type`=`ux.user_timing`, `emb.user_timing.name`, `.start_time` (ms from zero time), `.duration` (0), `.entry_type`=`mark`, body = JSON detail when present; a **measure** becomes a span named after the measure, from its start to start+duration, with `emb.type`=`ux.user_timing`, `emb.instrumentation`=`user_timing`, `emb.user_timing.entry_type`=`measure`, `.start_time`, `.duration`, `.detail` (JSON string when present). Deduplicated on `location.href::name`; 200 marks and 100 measures per part.
- Element timing (scope `ElementTimingInstrumentation`/`1.0.0`, Chromium only): span named after the `elementtiming` identifier, from zero time to the render `startTime`; `emb.type`=`ux.element_timing`, `emb.element_timing.identifier`, `.element` (tag), `.render_time`, `.load_time`, `.start_time`, `.url`, `.natural_width`, `.natural_height`; span event `load` at loadTime when >0. 100 per part.
- Server timing (scope `ServerTimingInstrumentation`/`1.0.0`): read once per hard load from the navigation entry's `serverTiming`; one log `emb-server-timing` per entry (INFO, no timestamp) with `emb.type`=`ux.server_timing`, `emb.server_timing.name`, `.duration` (number), `.description`. 50 per part.

## DOM state (`instrumentations/dom-state`, scope `DOMStateInstrumentation`/`1.0.0`)

- One `dom-state` log (INFO, `emb.type`=`emb.otel_log`) at **every part end**: `dom_state.element_count` (int) and `dom_state.average_depth` (double), or only `dom_state.traversal_limit_reached=true` past 50,000 elements; `dom_state.document_height`, `dom_state.document_width` (ints).
- Once per page, on the first part-end log after the load event: `dom_state.images_above_fold.count`, `.viewport_height`, `.viewport_width`, `.timestamp` (epoch ms), `.capture_time` (ms from zero time).

## Interactions

- Clicks: span event `click` on the part span (timestamp = event time), attributes `emb.type`=`ux.tap`, `tap.coords`=`x,y`, `view.name`=`<{tag}{ class="..." when className non-empty}>{innerText first 30 chars}{"..." if longer}</{tag}>`, where the element is the innermost element clicked (often a `<span>` inside a `<button>`); no id. Disabled elements and SVG are skipped. Cap: 200 events per part span (shared with breadcrumbs).
- First interaction (scope `FirstInteractionInstrumentation`/`1.0.0`): log `first-interaction` once per part on the first click, keydown or scroll: `first_interaction.interaction_type` (`tap` when pointerType is touch, `click`, `keypress`, `scroll`), `.element_type` (lowercased tag, `document` for scroll), `.element_selector` (getSelector; `''` for scroll), `.time` (ms from zero time), `.x`/`.y` (clicks only).
- getSelector: walk up from the node; each part `#id` (stop there) or `tag.class1.class2` with classes sorted; joined `ancestor>descendant`; at most 99 characters.
- Max scroll depth (scope `MaxScrollDepthInstrumentation`/`1.0.0`): log `max-scroll-depth` at every part end: `max_scroll_depth.pixels` (int), `.did_scroll` (bool), `.percent` (int, `min(100, round(maxY/scrollable*100))`, 0 when not scrollable), `.document_height` (int).
- Rage click (scope `RageClickInstrumentation`/`1.0.0`): ≥3 clicks within 500 ms on the same target or within 50 px of the first; one log `rage-click` per window, timestamp = first click: `rage_click.element_type`, `.element_selector`, `.x`, `.y`, `.count` (int), `.interaction_type` (`tap`/`click`).
- Breadcrumbs: event `emb-breadcrumb` with attribute `message` on the part span; 100 per part.
- `user.id` comes from `setUserId` (localStorage per origin) and appears on spans and logs only when set.

## Logs and exceptions (`managers/EmbraceLogManager/EmbraceLogManager.ts`)

- `emb-log`: severity INFO/WARN/ERROR (9/13/17) with text `INFO`/`WARNING`/`ERROR`; body = trimmed message (≤4000 chars); `emb.type`=`sys.log`, `emb.state` (`foreground`/`background`); `emb.stacktrace.js` and `emb.js_file_bundle_ids` only for warning and error logs (stack from `new Error().stack`, Chromium starts with `Error`); custom attributes as strings.
- `emb-exception`: severity 17 `ERROR`; body = message (≤1024); `emb.type`=`sys.exception`; `emb.exception_handling` = `handled` (`logException`, hidden by the JavaScript Exceptions list's default filter), `unhandled_error` (window error), `unhandled_rejection`, `unhandled` (React error boundary, with `react.component_stack` and `emb.instrumentation`=`react_error_boundary`); `emb.exception_cause` (`present` or `''`); `exception.type` = constructor name; `exception.name` = `error.name`; `exception.message`; `exception.stacktrace` = `error.stack`; `emb.js_file_bundle_ids`; `emb.state`; `emb.exception_number` (int, per origin, persists across page loads and tabs).
- A cross-origin `Script error.`: type and name `String`, message `Script error.`, stacktrace `''`, handling `unhandled_error`.
- Stacks: Chromium `Name: message\n    at fn (url:line:col)`; Firefox and WebKit `fn@url:line:col` with no header (WebKit shows `[native code]` frames). The backend picks the parser from the first matching line, so a Chromium message must not contain `@` (`go/lib/codec/js.go`).
- `emb.js_file_bundle_ids`: `JSON.stringify(globalThis._EmbraceFileBundleIDs || {})`; with the web CLI each key is the `Error().stack` captured when a bundle loads and each value its bundle ID (the source map `debugId` without hyphens, else the MD5 of the JS). Without the CLI it is `'{}'` (`packages/web-cli/src/processSourceFiles.ts`). Source maps must be uploaded (`POST /v2/store/sourcemap`) before exceptions arrive; later uploads do not regroup stored exceptions.
- Issue grouping: hash of `exception.name` plus the chosen frame's method and file (`go/domain/crash/id.go:330-337`).

## Limits per part (`managers/EmbraceLimitManager/constants.ts`; reset at each part end)

error_log 500, warning_log 200, info_log 100, exception 500, breadcrumb 100, session_property 100, span 1000, network_request 10000, user_timing_mark 200, user_timing_measure 100, element_timing 100, server_timing 50, soft_navigation 100.

## Capability gating (SDK feature tests mapped to engines)

| Signal | SDK test | Chromium | WebKit (Safari) | Gecko (Firefox) |
| --- | --- | --- | --- | --- |
| TTFB, FCP, LCP | navigation entry, `paint`, `largest-contentful-paint` | yes | yes (goldens) | yes (goldens) |
| CLS | `layout-shift` | yes | no | no |
| INP | `PerformanceEventTiming` with `interactionId` | yes | unverified: do not emit | unverified: do not emit |
| LoAF / TBD | `long-animation-frame` | 123+ | no | no |
| Element timing | `element` | yes | no | no |
| Navigation API rollover | `window.navigation` | 102+ | 26.2+ | 147+ |
| Soft-navigation span, native | `soft-navigation` entry | 151+ | no | no |
| Soft-navigation span, polyfill | Navigation API + `event` entry type | <151 with Nav API | no (no Event Timing) | 147+ |
| Soft-navigation vitals | native soft navigations | 151+ | no | no |
| `device_memory`, `network_effective_type` | resource detection | yes | no | no |
| render_blocking_status, delivery_type, not_restored_reasons | resource timing fields | yes | no | no |
| resource `http.response.status_code` | `responseStatus` | yes | no | yes |

## Backend facts the generator depends on

- URL rollup (`go/lib/grouper/classify.go`, `const.go`): numbers, dates/timestamps, UUIDs (32 hex or 36 with dashes), hex strings longer than 12 characters, slugs with ≥2 dashes and text ≥64 characters collapse immediately; other segments collapse after 25 distinct values at one level. Surfaces are not rolled up; Pages health counts views per `app.surface.name` (`go/lib/codec/session_surface_mapper.go:31-90`).
- Web ingest drops: payloads whose Origin is not on the app's allowed origins (when enforcement is on, `go/lib/nginx/processor/processor.go:443-480`), `/v2/spans` payloads without a `ux.session_part` span, resources whose parent document load is not in the same payload (`go/loader/session/web-resource/internal/inserter/inserter.go:86-112`), `file://` URLs and SpeedCurve PTST traffic. Vitals are not deduplicated (`go/domain/webvital/table/table.go:13`).
- Page Load Insights defaults to Chrome 107+ desktop and lists only `link`, `script`, `css`, `img` and `video` initiators.
- Forwarded client spans are named `lower(app name with spaces as underscores)-<platform>-<environment>` (`go/lib/common/data_destinations_config.go:114`): app `kfs9f` "Vantra Web" forwards as `vantra_web-web-production`.

## Where V2-BUILD-PROMPT.md is wrong or imprecise (the SDK wins)

- `emb.was_from_inp` (section 7.3) does not exist; do not emit it.
- INP's body includes `interactionTarget`; `element_type` and `loaf_scripts` are attributes only when available.
- Server timing logs have no timestamp and are read once per hard load.
- DOM state is logged at every part end, not once per page; only the above-the-fold fields are once per page.
- Firefox's modern TypeError text is `can't access property "price", t is undefined`, not `t is undefined`.
- Log batching delay is the OTel default (1 s), not V1's 5 s.
- The SPA estate in the prompt put the travel booking flow on app.vantra.com; a cross-origin hop would split the user session and device ID (separate storage), so V2 keeps travel's hybrid site on one origin (www.vantra.com, booking flow under `/travel/book`).
