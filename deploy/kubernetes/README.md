# Kubernetes package

One container continuously generates web, Android and iOS sessions. The optional UI is included. Use the [deployment handoff](../../docs/kubernetes-handoff.md) for the operational contract.

## Configure

Copy this directory to the company configuration repository, for example `load/session-generator`. Keep real secrets in its existing SOPS/KSOPS environment-secret flow.

1. Edit `overlays/company/config.env`: fill each app ID to enable, leave the others blank, and set app names/regions and the **total** requested rate. At least one ID is required. There are deliberately no working example IDs in the deployment.
2. In `overlays/company/kustomization.yaml`, set the existing workload namespace and the published image digest. Replace `newTag` with `digest: sha256:<release-digest>`. The namespace must already exist and be permitted by the ArgoCD project.
3. Select a CSI block-backed filesystem StorageClass for the PVC. The default class must support `ReadWriteOncePod`; otherwise add a StorageClass patch. See storage requirements below before choosing a fallback.
4. For backend traces, uncomment the `chronosphere` component and map its two `secretKeyRef` entries to the existing tenant Secret's actual name/keys. For optional web source maps, enable `web-symbols` and map that separate credential. Placeholder key names (`orgName`, `apiToken`, `symbolUploadToken`) are examples, not a claim about the company schema. The referenced Secrets must exist in the workload namespace before startup. Selected references are required, never silently optional.
5. Render with the company's pinned Kustomize/KSOPS setup and review the GitOps change. The application-only package can be checked with:

   ```sh
   kubectl kustomize deploy/kubernetes/overlays/company
   ```

The package renders only a Deployment, ConfigMap and PVC. It creates no Secret, Namespace, public Service, Ingress, schedule or cluster permissions. **Syncing a configured deployment starts live synthetic traffic.** Building or rendering it does not.

The image needs no GCP SDK, SOPS or KSOPS. ArgoCD's rendering identity decrypts secrets; Kubernetes injects only the explicitly selected keys. Do not put keys into `config.env`, image build arguments, shell commands or CI logs. Do not upload decrypted company manifests as build artifacts.

## Storage and updates

Keep one replica with `Recreate`. The PVC is 20 GiB by default; the pod requests 1 CPU/1 GiB and permits 2 CPU/4 GiB. These are starting allocations, not throughput guarantees. No HPA. SQLite WAL and file locks require local filesystem semantics: no NFS/RWX multi-host volume.

Prefer `ReadWriteOncePod`. If the CSI driver supports only `ReadWriteOnce`, Charles must enforce a single writer: RWO restricts nodes, not pods. `Recreate` prevents ordinary rollout overlap but cannot fence a worker on an unreachable node. Fence the old worker before forced replacement; local file locks are not distributed leadership.

`fsGroup: 10001` makes a fresh volume writable; `OnRootMismatch` avoids unnecessary recursive permission changes on replacement. Verify the actual CSI driver's behavior. Existing token files must remain UID 10001-owned, mode 0600/0400 after remount. Some drivers handle group ownership themselves. If necessary, use an approved, narrowly scoped ownership initializer; do not loosen token permissions or use `chmod 777`.

Nonsecret configuration changes alter the generated ConfigMap name and trigger pod replacement. Same-name Secret changes do **not** update a running process: also bump `session-generator/configuration-revision` in the pod template, or change a versioned Secret reference. Normal SIGTERM allows up to 90 seconds to drain. Do not add a `preStop` operator `stop`: that deliberately persists a stopped state.

Test the intended namespace, Secret bindings, storage permissions and pod replacement in the target cluster before sustained traffic. Local rendering does not validate company KSOPS/KMS permissions or CSI behavior.
