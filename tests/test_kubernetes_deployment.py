"""Render the deployable package; never contact a cluster or send telemetry."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

try:
    import yaml
except ImportError:
    yaml = None


ROOT = Path(__file__).resolve().parents[1]
KUBECTL = shutil.which("kubectl")


@unittest.skipUnless(KUBECTL and yaml, "Kubernetes rendering requires kubectl and PyYAML")
class KubernetesDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.package = Path(self.temp.name) / "kubernetes"
        shutil.copytree(ROOT / "deploy/kubernetes", self.package)
        self.overlay = self.package / "overlays/company"

    def render(self):
        result = subprocess.run([KUBECTL, "kustomize", str(self.overlay)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        resources = list(yaml.safe_load_all(result.stdout))
        self.assertEqual({r["kind"] for r in resources},
                         {"Deployment", "PersistentVolumeClaim", "ConfigMap"})
        self.assertEqual(len(resources), 3)
        self.assertTrue(all(r["metadata"]["namespace"] == "session-generator" for r in resources))
        return {r["kind"]: r for r in resources}

    def test_default_package_is_private_hardened_and_single_writer(self):
        rendered = self.render()
        deployment = rendered["Deployment"]["spec"]
        pod = deployment["template"]["spec"]
        container = pod["containers"][0]
        self.assertEqual(deployment["replicas"], 1)
        self.assertEqual(deployment["strategy"], {"type": "Recreate"})
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertEqual(pod["terminationGracePeriodSeconds"], 90)
        self.assertEqual(pod["securityContext"]["runAsUser"], 10001)
        self.assertEqual(pod["securityContext"]["fsGroupChangePolicy"], "OnRootMismatch")
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        self.assertFalse(container["securityContext"]["allowPrivilegeEscalation"])
        self.assertEqual(container["securityContext"]["capabilities"]["drop"], ["ALL"])
        self.assertNotIn("lifecycle", container, "Operator stop must not run during maintenance shutdown")
        self.assertNotIn("envFrom", container)
        self.assertEqual(rendered["PersistentVolumeClaim"]["spec"]["accessModes"], ["ReadWriteOncePod"])
        self.assertEqual({v["mountPath"] for v in container["volumeMounts"]},
                         {"/tmp", "/var/lib/session-generator"})
        self.assertTrue(any(v.get("emptyDir", {}).get("sizeLimit") == "256Mi" for v in pod["volumes"]))

    def test_probe_host_and_local_health_paths_are_preserved(self):
        container = self.render()["Deployment"]["spec"]["template"]["spec"]["containers"][0]
        for name, path in (("startupProbe", "/livez"), ("livenessProbe", "/livez"),
                           ("readinessProbe", "/readyz")):
            probe = container[name]["httpGet"]
            self.assertEqual(probe["path"], path)
            self.assertEqual(probe["port"], "http")
            self.assertIn({"name": "Host", "value": "localhost"}, probe["httpHeaders"])

    def test_config_is_explicit_and_changes_roll_out_without_keys_in_configmap(self):
        first = self.render()
        config = first["ConfigMap"]
        self.assertEqual(config["data"]["SG_RATE_PER_MINUTE"], "10")
        for platform in ("WEB", "ANDROID", "IOS"):
            self.assertEqual(config["data"][f"SG_{platform}_APP_ID"], "")
        self.assertFalse(any("TOKEN" in key for key in config["data"]))
        path = self.overlay / "config.env"
        path.write_text(path.read_text().replace("SG_WEB_APP_ID=\n", "SG_WEB_APP_ID=web01\n"))
        second = self.render()
        self.assertNotEqual(config["metadata"]["name"], second["ConfigMap"]["metadata"]["name"])
        container = second["Deployment"]["spec"]["template"]["spec"]["containers"][0]
        environment = {item["name"]: item for item in container["env"]}
        self.assertEqual(environment["SG_REQUIRE_AUTH"]["value"], "1")
        for key in second["ConfigMap"]["data"]:
            reference = environment[key]["valueFrom"]["configMapKeyRef"]
            self.assertEqual(reference, {"name": second["ConfigMap"]["metadata"]["name"], "key": key})

    def test_optional_components_bind_only_selected_required_secret_keys(self):
        kustomization = self.overlay / "kustomization.yaml"
        with kustomization.open("a") as file:
            file.write("\ncomponents:\n  - ../../components/chronosphere\n  - ../../components/web-symbols\n")
        rendered = self.render()
        environment = rendered["Deployment"]["spec"]["template"]["spec"]["containers"][0]["env"]
        references = {item["name"]: item["valueFrom"]["secretKeyRef"] for item in environment
                      if "secretKeyRef" in item.get("valueFrom", {})}
        self.assertEqual(set(references), {"SG_CHRONOSPHERE_TENANT", "SG_CHRONOSPHERE_TOKEN",
                                          "SG_SYMBOL_UPLOAD_TOKEN"})
        self.assertEqual(references["SG_CHRONOSPHERE_TENANT"]["key"], "orgName")
        self.assertEqual(references["SG_CHRONOSPHERE_TOKEN"]["key"], "apiToken")
        self.assertEqual(references["SG_SYMBOL_UPLOAD_TOKEN"]["key"], "symbolUploadToken")
        self.assertTrue(all(not ref.get("optional", False) for ref in references.values()))


if __name__ == "__main__":
    unittest.main()
