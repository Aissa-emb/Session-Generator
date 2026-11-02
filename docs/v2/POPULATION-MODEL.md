# Visitor population model

The guided workflow now defaults to `population.model: "varied"` for standard and
regression traffic on Android, iOS and web. Existing saved specs that omit this
field retain the previous journey distribution. The explicit causal demo mode
keeps its separate scenario, cohort and timing contracts.

## Assessment of the older Web RUM demo proposal

The proposal improves the demo's **behavioral variety**, but its architecture and
all of its percentages should not replace the current engine. These are useful,
configurable demo assumptions, not measured production traffic or market shares.

| Proposal | Decision |
| --- | --- |
| Browsers, buyers, abandoners, searchers, bouncers and power users | Adopt the retail weights and real journey branches; add banking and travel intent models. |
| Human pacing and skewed session lengths | Adopt bounded, skewed reading pauses; retain per-character typing with varied timing. |
| Device and network distributions | Adopt explicit marginal shares and conditional network profiles. Fix the former browser/device weight multiplication. |
| Fresh browser contexts for every visitor | Keep stable visitor identities and returning visits. Fix the full pool becoming 100% returning. |
| A large share of slow 3G connections | Use a smaller tail by default; expose exact override weights in ScenarioSpec. |
| CPU slowdown rigidly tied to network speed | Model hardware independently; a slow radio is not a slow CPU. |
| More than 90% coverage for all Web Vitals | Reject. INP requires a qualifying interaction and supported instrumentation; native sessions never emit Web Vitals. |
| Random generic exceptions | Keep the existing causal faults, executable exception fixtures and matching source maps. |
| Browser coordinates for worldwide location | Keep honest metadata and real regional egress. Locale/market cannot change XCOR RUM's IP-derived location. |
| Replace the engine with a browser fleet | Preserve the current architecture and SDK serializers. |

INP eligibility follows the [metric's definition](https://web.dev/articles/inp).
[Browser emulation](https://playwright.dev/docs/emulation) supplies browser settings;
it does not move the generator's network egress.

## Default visit intents

Weights are percentages **within each business area**, before a selected fault
steers a visit. An intent to transact is not a successful transaction.

| Retail | % | Banking | % | Travel | % |
| --- | ---: | --- | ---: | --- | ---: |
| Browse products | 30 | Quick balance check | 35 | Look at fares | 35 |
| Buy | 20 | Review history | 30 | Search flights or hotels | 25 |
| Abandon cart | 20 | Transfer | 15 | Book | 12 |
| Search products | 15 | Abandon transfer | 10 | Abandon booking | 15 |
| Bounce | 10 | Download statement | 7 | View itinerary | 10 |
| Power user | 5 | Power user | 3 | Power user | 3 |

These deliberately make transactions common enough to demonstrate. Retail buyer
and power-user weights together represent 25% purchase intent, not an empirical
25% conversion rate. Edit `population.intent_mix.<business area>` to calibrate
against actual data. A selected “Banking & transfers” flow selects the business
area; it no longer forces every visitor to submit a transfer.

Retail visits browse different products, power users add multiple products, and
abandoners leave before payment. Banking quick checks stop at the account overview;
transfer abandoners reach review but never submit. Travel abandoners stop before
booking. Existing milestone names and trace relationships are preserved.

## Shared and native behavior

- Reading pauses use a bounded triangular distribution, usually 3–15 seconds.
- Hardware stays fixed for a visitor; network conditions can change between visits.
- Web defaults to 60% desktop / 40% mobile. Requested browser and device shares are
  jointly fitted over supported combinations; impossible combinations are rejected.
- Desktop connections: cable 55%, Wi-Fi 35%, slow 4G 8%, fast 3G 2%.
- Handheld connections: Wi-Fi 45%, fast 4G 25%, slow 4G 20%, fast 3G 8%, slow 3G 2%.
- A mature pool targets 45% returning visitors. The first visits are new; a visitor
  cannot return within 30 minutes, and a saturated pool does not force reuse.
- Native first visits and app-version changes start cold. Returning visitors with
  an existing process have a 35% cold-start probability. This is **not** a 35%
  whole-population cold-start target. Warm returns reuse the process but receive
  new SDK session IDs. A fatal process is not reused.
- Native eligible content stages have a 12% chance of a 6–45 second interruption.
  iOS foreground resumption creates a separate SDK session, so SDK session count
  can exceed simulated visit count.
- Budget hardware increases native startup and browser rendering/interaction work.
  RTT and bandwidth affect requests and browser loading, not synchronous INP work.
  First-party resource timing uses fixed bytes and available bandwidth.

The UI exposes the visitor pattern, returning share, returning cold-start share,
and native interruption share under **Customize → Visitor mix**. Native models,
OS versions, intent weights, network weights and market weights remain available
through ScenarioSpec. The prior distribution remains selectable.

## Measurement and compatibility

The preview report reads `emb.properties.*` from serialized SDK session spans,
and transaction attempts/successes from serialized network spans. It reports
intent proportions, duration p50/p75/p95, cold visits, returning visits, and the
actual funnel. `journey_steered` distinguishes visits extended for a requested
fault from natural intent. Small preview samples are not percentage guarantees.

Explicit fault quotas, browser capability gates, causal scenario graphs, source
maps, acknowledged delivery counters and approved UI styling are retained. The
conditional Web Vitals baseline is not advertised as a fixed aggregate p75;
explicit segment targets are still compared when configured.

Run the reproducible offline audit:

```sh
.venv/bin/python tools/population_audit.py /tmp/population-audit.json --count 1200 --seed 8219
.venv/bin/python -m unittest discover -s tests -p test_population.py -v
```

The audit generates 1,200 visits per platform over a fixed twenty-hour simulation,
validates the serialized payloads, and reports observed distributions. It performs
no remote sends. New runs use the new model; running jobs retain their saved spec.
Dashboard geolocation still requires regional egress, and iOS live ingestion still
requires a real iOS app ID.
