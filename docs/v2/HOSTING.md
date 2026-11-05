# Hosted control plane and streams

Use a Linux VM with Docker Engine and Compose, persistent storage, two CPU cores and 4 GiB RAM to start. Measure volume and memory before increasing rates or planning large backfills; a bounded job materializes its plan. The engine's ceiling is a guardrail, not a VM sizing recommendation.

## Local container

```sh
docker compose build
docker compose up -d
docker compose exec generator python -m server.admin create-token --name 'Workspace owner'
```

The last command displays a token once. Open `http://localhost:8000` and enter that token in the connection screen. Store it securely; the authenticated `DELETE /api/settings/tokens/{id}` endpoint can revoke it. It is hashed in the persistent database. The image runs as UID/GID 10001, the UI is built in its own Node stage, and no repository config file, environment file, private key or telemetry output is copied into the image. Compose exposes only host loopback, drops capabilities, uses a read-only root filesystem and retains data in `generator-state`.

The image sets `TMPDIR` to the writable data volume. Batch payloads and previews use private temporary directories there, rather than the 256 MiB `/tmp` mount. Keep at least 512 MiB free before staging; each batch is limited to 4 GiB of compressed payloads, and concurrent jobs need additional space. A hard kill can leave private scratch directories for operator cleanup after confirming no worker uses them. The CI acceptance volume is 1 GiB for its small dry runs; production Compose uses a disk-backed volume.

`docker compose logs -f generator` contains structured lifecycle/progress events with an explicit field allowlist. Request bodies, headers, credentials and remote exception bodies are excluded. `/health` checks database access; stream summaries report per-stream heartbeat health, UTC daily accepted/rejected record totals and delivery errors. Health alone does not prove Embrace visibility or trace retention.

## VM and credentials

Keep Compose's loopback binding. Use an SSH tunnel for private access, or place a TLS reverse proxy in front and set `SG_ALLOWED_HOSTS` to its exact public hostname. Keep `SG_REQUIRE_AUTH=1`. The proxy must pass the original Host and support streaming responses; no CORS configuration is needed. Use a firewall to expose only SSH and, if chosen, HTTPS. Do not publish port 8000 directly.

Container secrets may be supplied as environment variables or owner-readable mounted files:

| Secret | Environment variable | File reference |
| --- | --- | --- |
| Cortex XCOR | `SG_CHRONOSPHERE_TOKEN` | `SG_CHRONOSPHERE_TOKEN_FILE` |
| Embrace verification | `SG_EMBRACE_API_TOKEN` | `SG_EMBRACE_API_TOKEN_FILE` |

| Symbol upload | `SG_SYMBOL_UPLOAD_TOKEN` | `SG_SYMBOL_UPLOAD_TOKEN_FILE` |

Prefer mounted files owned by UID 10001, mode 0600; the existing token reader rejects symlinks and permissive modes. Never put credentials in the Compose YAML, image build args, scenario JSON or Git. The destination step writes secrets to `SG_SECRET_DIR` inside the private data volume; macOS development uses Keychain instead. Back up the data volume securely: it includes write-only secret files, token hashes, scenario versions, receipts and run journals.

The UI creates finite continuous runs with a selected duration. For an always-on managed stream, create the desired live scenario and explicitly confirm it through `POST /api/streams`. Active authorized streams resume after a process/container restart, starting new sessions without replaying incomplete deliveries. Explicitly paused or stopped streams remain stopped. Shutdown requests a pause and allows pending deliveries to drain for up to 65 seconds; Compose allows 90 seconds. A hard kill may interrupt a delivery, which must be reviewed rather than resent. Bounded jobs never auto-resume uncertain exports.

Use rolling **Last 24 hours** for backfilled sessions. Backend traces begin with the running stream and remain subject to the eight-minute safety window. Container uptime does not override ingestion or retention limits.

## Multiple regions

Use one independent deployment and persistent volume per actual cloud region, with `SG_RUNNER_REGION` labeling its operational status. Give each runner a separate named MCP/API token and an allocated fraction of the desired total rate. Do not share a SQLite database or persona file across machines. Embrace determines location from the sender's public IP: region labels and user agents cannot change it. Verify each runner's network egress and target readiness before enabling its stream.

## Owner-approved LaunchAgent cut-over

1. Keep the existing `~/xcor-demo-generator` checkout and LaunchAgent running until the owner approves a cut-over window.
2. Build and test the new deployment with dry runs, then configure the approved apps, tenant, forwarding domains, retention and allowed Origins using the setup kit.
3. With explicit owner approval, send a small probe from the new runner, wait at least five minutes, and verify every known trace ID plus its app-rooted parent chain.
4. Record the old LaunchAgent label, configuration, rate, last receipt time and restart command. Obtain approval to stop it; do not run two full-rate production streams unintentionally.
5. Pause the approved old agent, start the new stream at the intended rate, and perform the 30-minute live acceptance in `CODEX-HANDOFF.md` section 5.
6. If acceptance fails, pause the new stream and restore the old agent using the recorded command. Preserve both sets of receipts for diagnosis; never replay uncertain deliveries.

No cut-over, tenant changes or live telemetry are performed by the automated test suite.
