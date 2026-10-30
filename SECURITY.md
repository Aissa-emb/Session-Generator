# Security boundaries

This is an outbound telemetry generator, not a banking, shopping, or travel server. SQL, Redis commands, authentication flows, and stack traces in profiles are strings describing fictional telemetry. They are not executed against databases or real customer accounts. The generator does not expose a network listener. Its configuration and local checkout must be trusted.

## Credentials

- Store ingestion tokens outside the repository in an owner-only file. Reference it with `chronosphere.api_token_file` or `CHRONOSPHERE_API_TOKEN_FILE`. Environment injection via `CHRONOSPHERE_API_TOKEN` is also supported. Token files must be regular files, not symlinks, and owned by the current user with mode 600 or 400 on POSIX systems.
- Inline secrets remain supported only in private config files. Do not place tokens in command arguments, shell history, endpoint URLs, payload fixtures, or issue reports. `.gitignore` is a precaution, not protection for files already committed.
- Use a service account limited to trace ingestion. Trace queries and administrative changes may require a different credential. Anyone who can read the token or execute code as the generator's OS user can use its permissions.
- If a real credential is ever committed or otherwise disclosed, revoke/rotate it at the provider and update the local secret. Deleting the text or rewriting history does not invalidate the old credential.

- V2 specs never contain secret values. `chronosphere.token_ref` names where the token lives: `keychain:<service>[/<account>]` (macOS Keychain), `file:<absolute path>` (owner-only file, same rules as above) or `env:<VARIABLE>`. Specs, presets and run manifests are safe to share; the referenced secret is not.

## Network and local data

HTTPS certificate verification remains enabled. Remote HTTP endpoints, URL credentials, query strings, fragments, header injection, and redirects are refused. Plain HTTP is permitted only for a local loopback collector without credential or identifying headers. Proxy configuration and the OS trust store remain trusted deployment settings.

Export responses are capped at 1 MiB and trace query responses at 32 MiB. Malformed protobuf responses are rejected with bounded varint parsing. HTTP error bodies and OTLP error messages are not written to logs. The sender bounds outstanding network tasks and retries transient failures, including HTTP 429, a limited number of times; persistent failures still require operator action. Generated telemetry may contain fictional user identifiers; state files use private atomic writes. Run as an ordinary dedicated OS user, not root.

## Checks

```bash
python3 -m unittest discover -s tests -v
gitleaks git . --config .gitleaks.toml --log-opts=--all --redact=100
bandit --ini .bandit -r xcorgen generate.py tools
```

GitHub Actions scans full history and runs security regression and payload validation checks on pushes and pull requests. The checkout action is pinned to a commit, Gitleaks is pinned by version and SHA-256, and the job receives read-only repository permission without persisted checkout credentials. GitHub's built-in secret scanning may require a separate plan/setting and is not implied by this workflow.

The Gitleaks exception covers only the literal `emb.w3c_traceparent` attribute name in three captured SDK files and the scrubbed 2.29 web session sample. It does not exempt credential values or other files. Bandit's B311 check is disabled because seeded pseudo-random values are used only for simulation; the deterministic MD5 SDK build label is explicitly marked as non-security use.

A clean scan is evidence for these checks, not a guarantee that the code, host, dependencies, or connected services cannot be compromised. Keep Python and the operating system patched, and repeat the checks when code or deployment settings change.


### Browser exports

Web targets default to omitting `Origin`. The `allowed` policy sends the configured value, and `site` sends the current estate origin. API trace propagation is limited to the target's explicit host allow-list. SDK uploads and third-party requests never carry trace context. Browser fault inputs use catalog families with engine-specific stacks; the realism linter checks generated payloads and the browser capability/lifecycle contract. Only the anonymized web sample under `reference/` is committed.

Backend fault controls accept bounded retry and Kafka-delay values. Service-version routing metadata is internal and is excluded from emitted attributes. Fault construction performs no network access.

Run journals and manifests are owner-only, reject symbolic links, and contain no credentials. Resume checks the normalized spec digest and dry/live mode. Network ambiguity and OTLP partial success block automatic resends; exactly-once remote ingestion cannot be promised without a transactional ingest API. Strict journaled delivery disables transport retries.

### Control-plane security

The server defaults to loopback, checks Host and Origin to prevent DNS rebinding/CSRF, bounds request bodies to 1 MiB and requires hashed bearer tokens for remote API and MCP access. Set allowed public hostnames explicitly and terminate TLS before exposing a hosted deployment. Token plaintext is returned once at creation; hashes and secret values are excluded from listing endpoints. Validation errors omit submitted input values. Secrets use Keychain APIs or private mounted files, never shell arguments. Job subprocess arguments contain only a run ID; scenario data reaches the preview worker over stdin. Provider response bodies and unexpected exceptions are suppressed. Read-only Embrace MCP calls are allow-listed and schema-validated.

API jobs use exclusive OS locks and SQLite transactions; stale workers are marked for recovery rather than automatically duplicated. Local ingest tests create a temporary certificate and trust it explicitly, without disabling certificate verification.

The built UI serves bundled scripts/fonts only. Access tokens entered for remote API access live in session storage and are never included in scenario specs. API calls use same-origin authentication and server validation; UI checks are advisory. Playwright uses a separate local database and dry-run worker. Browser dependencies are locked; npm audit and generated-contract drift checks run in CI.

MCP HTTP requires bearer authentication on loopback and remotely; Host/Origin defenses and the 1 MiB request bound also apply. Stdio grants the invoking OS user's local application access. Copilot has no run, configuration, upload or secret tools. User/scenario fields are data, and model output must pass the same schema, capabilities, limits and realism checks as hand-written specs. A proposal cannot apply over a newer version. Live model calls require an explicitly configured key; response fixtures contain no real account data.

Preparation plans have no outbound effect until an explicit boolean `confirm: true`. Live readiness and credentials are checked before the first stage; local dry runs identify their readiness as simulated. Stage idempotency keys and process locks prevent duplicate starts after orchestration interruption. A cancellation is serialized with stage transitions and requests a stop on every created job. Resuming uncertain telemetry delivery remains blocked by the delivery journal.
