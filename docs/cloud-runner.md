# Cloud runner

Run Session Generator continuously on one Linux VM or company server. Supply app IDs and optional keys once; the runner builds its configuration internally. Web, native Android and native iOS can run individually, in any pair, or together. No scenario file, UI setup or laptop process is required.

## What you supply

| Input | When needed |
| --- | --- |
| Web app ID | To generate web sessions |
| Android app ID | To generate native Android sessions |
| iOS app ID | To generate native iOS sessions |
| Chronosphere tenant and API token | Together, to add linked backend traces from every enabled platform |
| Web Symbol Upload key | Optional, to resolve generated JavaScript stacks to original files and lines |

Each enabled platform needs its own existing five-character app ID. Omitting an ID excludes that platform; no separate platform switches are needed. A Symbol Upload key applies to web only. Sessions do not require an XCOR RUM service-account token; that credential is for optional verification, not ingestion.

The defaults are 10 **total** sessions/minute with equal platform weights, running until explicitly paused or stopped. The current UI Production baseline supplies the error population, varied devices and journeys. Names default to Vantra Web for web and Vantra for native apps; ingest defaults to US. These are generator assumptions, not values inferred from your app IDs. A requested rate is an average, not a promise of an exact count each minute.

## One-time infrastructure setup

The infrastructure owner provides:

- One Linux host with Docker Engine, the Docker Compose plugin and Python 3. Start with two CPU cores and 4 GiB RAM; measure achieved rate and memory before increasing traffic.
- A durable local filesystem for `/srv/session-generator`. Mount a persistent disk there when the host may be replaced. Do not share this SQLite state directory between hosts or replicas.
- A versioned container image from this checkout or your approved registry. Use a commit-specific immutable tag or digest, not `latest`.
- Outbound HTTPS to the selected XCOR RUM ingestion endpoints, Chronosphere when enabled, and the symbol upload service when enabled.
- Private administrative access. Port 8000 stays bound to loopback; no public load balancer or public domain is required.

The same setup works on cloud VMs and company infrastructure. The container needs an always-running host; a request-only serverless deployment or multiple replicas is not this deployment model. Enable Docker's startup at host boot using your host management tooling; Compose's restart policy brings the container back when Docker starts. Existing operator pause/stop is preserved.

From the repository on that host:

```sh
docker build -t "session-generator:$(git rev-parse --short=12 HEAD)" .
sudo python3 deploy/cloud/setup.py
```

Use the image tag from the build in the setup prompt. Enter all three app IDs for an all-platform runner; leave IDs blank for platforms you do not want. The optional tenant prompt enables backend traces and asks for its key. The optional web Symbol Upload key is entered separately with hidden input.

Setup writes non-secret inputs into `/srv/session-generator/.env`, creates the persistent `data/` directory and stores credentials under `credentials/`. Credential files are owned by the container's UID/GID 10001 with mode 0600; directories are private. Neither setup nor building an image sends telemetry. Setup does not change a running container.

The resulting non-secret app inputs look like this; users do not need to author this file:

```dotenv
SG_WEB_APP_ID=<your-web-app-id>
SG_ANDROID_APP_ID=<your-android-app-id>
SG_IOS_APP_ID=<your-ios-app-id>
SG_CHRONOSPHERE_TENANT=<your-tenant>
```

For unattended infrastructure provisioning, your secret manager can populate the same private mounts. The env file uses `SG_CHRONOSPHERE_TOKEN_FILE=/run/secrets/<filename>` and optional `SG_SYMBOL_UPLOAD_TOKEN_FILE=/run/secrets/<filename>`; the Compose file mounts `SG_HOST_SECRETS_DIR` read-only at `/run/secrets`. Both credentials are optional for sessions-only operation. Grant the host access only to its required secret versions and container image. Do not put raw credentials in metadata, source control, image layers, command arguments or logs. The application rejects symlinked token files, wrong ownership and overly permissive file modes.

## Check and start

```sh
sudo deploy/cloud/runner.sh check
sudo deploy/cloud/runner.sh start
sudo deploy/cloud/runner.sh status
```

`check` uses the selected image, configured IDs and mounted files to generate 12 sessions locally. It has **no network**, no published port and temporary state separate from the runner. It validates local configuration and payload generation; it cannot prove a remote key is valid or a destination retains data. `start` repeats this check before starting the application.

**Starting this cloud launcher authorizes continuous synthetic traffic to the configured apps and tenant.** It also authorizes automatic source-map upload when the web symbol key is supplied. The cloud Compose file sets `SG_CLOUD_RUNNER=1`; the ordinary `docker-compose.yml` and local UI startup remain interactive.

For web with a symbol key, the generator prepares its matching assets/maps and records successful uploads before starting symbolicated traffic. Previously acknowledged uploads are reused. A rejected key or failed requested upload blocks startup with an error. Without the optional key, sessions still send real captured compiled stacks; original-source mapping is not enabled.

The cloud controller owns one stream. Repeat startup recovers that stream rather than launching a second one. Unexpected worker exits receive bounded recovery; delivery circuit-breaker failures and explicit operator stops are not silently restarted. Existing UI jobs are not adopted by cloud startup.

## Monitor and control

```sh
sudo deploy/cloud/runner.sh status
sudo deploy/cloud/runner.sh logs
sudo deploy/cloud/runner.sh pause
sudo deploy/cloud/runner.sh resume
sudo deploy/cloud/runner.sh stop
```

Status shows the cloud stream's state, destinations, requested and achieved rate, acknowledgement counts and failure information. API acknowledgement is not verified storage or dashboard visibility. `/health` checks process/database availability, so infrastructure should also inspect cloud status when monitoring generation. The authenticated HTTP equivalent is `GET /api/cloud/status`.

Pause halts new sessions and lets pending deliveries finish at their scheduled times; stop flushes pending deliveries immediately. Both operator choices persist across restarts; explicitly resume when ready. Maintenance shutdown also flushes pending work, while preserving the operator's running/paused/stopped choice. Commands queue the desired state for the existing controller, so inspect `status` to see the resulting transition. They do not launch a competing controller. The API equivalents are authenticated `POST /api/cloud/pause`, `/stop` and `/resume`.

`sudo deploy/cloud/runner.sh down` shuts down the container. It preserves data and the stream's operator state; a previously running stream recovers on the next `start`. To keep generation stopped after subsequent startup, use `stop` first and check its state before `down`.

For optional UI access, create a token on the host:

```sh
sudo docker compose --project-name session-generator-cloud \
  --env-file /srv/session-generator/.env -f docker-compose.cloud.yml \
  exec generator python -m server.admin create-token --name 'Cloud operator'
```

The token is shown once; store it securely. From your workstation, open a private tunnel:

```sh
ssh -N -L 8000:127.0.0.1:8000 your-runner-host
```

Open `http://127.0.0.1:8000` and enter the token. Cloud generation does not depend on this browser login. Use the cloud status command/API for its owned stream; the existing UI workflow remains available for interactive jobs. Keep the total traffic from any extra UI jobs in mind.

## Adjust inputs or update the image

To replace IDs, keys or the image tag, rerun setup with `--replace`, then update:

```sh
sudo python3 deploy/cloud/setup.py --replace
sudo deploy/cloud/runner.sh update
sudo deploy/cloud/runner.sh status
```

Supply the complete desired set again; omitted IDs/keys are omitted from the replacement configuration. Setup writes new credential versions without overwriting files an existing run may still use. `update` runs the offline check first, gracefully stops the old container and starts its replacement with the same persistent state. Invalid new configuration does not stop the currently running container. Do not run old and new hosts concurrently against the same workload.

Keep the previous versioned image available for rollback. The launcher never deletes the state directory or old credential versions. Remove obsolete secret files only after confirming no active or paused run uses them. Backups include sensitive private state and must be protected. Stop the container before taking a filesystem copy, or use a database-aware backup method that includes SQLite WAL state.

Optional non-secret overrides in the protected env file:

| Variable | Default |
| --- | --- |
| `SG_RATE_PER_MINUTE` | `10`, total across enabled platforms |
| `SG_WEB_APP_NAME` | `Vantra Web` |
| `SG_ANDROID_APP_NAME`, `SG_IOS_APP_NAME` | `Vantra` |
| `SG_WEB_INGEST_REGION`, `SG_ANDROID_INGEST_REGION`, `SG_IOS_INGEST_REGION` | `us` |
| `SG_RUNNER_REGION` | `cloud`, an operational label |

Run `update` after editing. To use a different persistent directory, setup accepts `--directory /your/path`; pass `--env-file /your/path/.env` before the launcher command on every invocation.

## Limits to understand

- Source-map setup is automatic only for the generator's own web bundles. This does not upload native Android/iOS symbols or arbitrary application builds.
- Apps must exist in the correct ingest region. Linked frontend/backend traces still require app forwarding, forwarded domains and destination retention configuration. Keys alone do not configure those integrations.
- Geographic attributes do not change the host's real egress IP. Destination location can still reflect that IP.
- Shutdown allows up to 65 seconds for worker drain; the container receives a 90-second grace period. A forced stop can interrupt delivery. The runner does not replay uncertain live uploads or promise exactly-once/lossless recovery.
- Recent receipt/event details are bounded; cumulative acknowledgement counts remain. Historical jobs, saved scenarios and backups still require an operator disk-retention policy. See [production limits](v2/PRODUCTION-HARDENING.md).

Local checks and automated tests never send to your real apps. Remote credential acceptance, source-map upload, forwarding and dashboard retention can only be established after an authorized live start and destination-side verification.

## Developer container acceptance

This finite check exercises real worker processes, exclusive ownership, maintenance restart, and durable pause/stop using fake app IDs and dry-run delivery. A second container verifies persistence. Use a new test volume; never mount production state or pass real credentials.

```sh
docker build -t session-generator:cloud-check .
smoke_volume="session-generator-smoke-$(date +%s)"
docker volume create "$smoke_volume"
docker run --rm --network none --read-only --init --cap-drop ALL \
  --security-opt no-new-privileges:true \
  -v "$smoke_volume:/var/lib/session-generator" \
  --tmpfs /tmp:rw,noexec,nosuid,size=256m,uid=10001,gid=10001,mode=0700 \
  session-generator:cloud-check python -m tools.cloud_smoke
docker run --rm --network none --read-only --init --cap-drop ALL \
  --security-opt no-new-privileges:true \
  -v "$smoke_volume:/var/lib/session-generator" \
  --tmpfs /tmp:rw,noexec,nosuid,size=256m,uid=10001,gid=10001,mode=0700 \
  session-generator:cloud-check python -m tools.cloud_smoke --verify-stopped
docker volume rm "$smoke_volume"
```

Both invocations must report success. The smoke tool refuses live/history state and active external networking. To inspect a failure, retain this isolated test volume until diagnosis is complete, then remove it.
