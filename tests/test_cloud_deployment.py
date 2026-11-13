"""Cloud packaging checks; no daemon, real keys, or outbound receivers required."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
MODULE = importlib.util.spec_from_file_location("cloud_setup", ROOT / "deploy/cloud/setup.py")
setup = importlib.util.module_from_spec(MODULE)
MODULE.loader.exec_module(setup)


class CloudDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/private/tmp" if Path("/private/tmp").is_dir() else None)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.app_ids = {"web": "web01", "android": "and01", "ios": "ios01"}

    def write_config(self, **overrides):
        args = dict(directory=self.directory / "runner", image="session-generator:abc123", app_ids=self.app_ids,
                    tenant="", trace_token="", symbol_token="")
        args.update(overrides)
        with patch.object(setup.os, "chown") as chown, patch.object(setup.os, "fchown") as fchown:
            result = setup.write_setup(**args)
        return result, chown.call_args_list, fchown.call_args_list

    def test_all_platform_setup_keeps_keys_out_of_env_and_versions_files(self):
        env, ownership, file_ownership = self.write_config(tenant="example", trace_token="fixture-trace-key",
                                                         symbol_token="fixture-symbol-key")
        text = env.read_text()
        self.assertNotIn("fixture-trace-key", text)
        self.assertNotIn("fixture-symbol-key", text)
        for platform, app_id in self.app_ids.items():
            self.assertIn("SG_" + platform.upper() + "_APP_ID=" + app_id, text)
        self.assertTrue(any(call.args[1:] == (10001, 10001) for call in ownership))
        self.assertTrue(any(call.args[1:] == (10001, 10001) for call in file_ownership))
        credentials = env.parent / "credentials"
        originals = {path.name: path.read_text() for path in credentials.iterdir()}
        self.assertEqual(len(originals), 2)
        for path in (env, *credentials.iterdir()):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        for name in ("data", "credentials"):
            self.assertEqual((env.parent / name).stat().st_mode & 0o777, 0o700)
        self.write_config(tenant="example", trace_token="fixture-replacement", replace=True)
        for name, value in originals.items():
            self.assertEqual((credentials / name).read_text(), value)
        self.assertEqual(len(list(credentials.iterdir())), 3)

    def test_sessions_only_setup_needs_no_keys_and_no_implicit_platform(self):
        env, _, _ = self.write_config(app_ids={"ios": "ios01"})
        text = env.read_text()
        self.assertIn("SG_WEB_APP_ID=\n", text)
        self.assertIn("SG_ANDROID_APP_ID=\n", text)
        self.assertIn("SG_IOS_APP_ID=ios01\n", text)
        self.assertEqual(list((env.parent / "credentials").iterdir()), [])
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.write_config()

    def test_invalid_input_cannot_write_configuration(self):
        for values in (
            {"app_ids": {}}, {"app_ids": {"web": "bad"}},
            {"app_ids": {"web": "same1", "ios": "same1"}},
            {"app_ids": {"ios": "ios00"}}, {"image": "session-generator:latest"},
            {"image": "registry:5000/image"}, {"tenant": "example"},
            {"trace_token": "fixture-without-tenant"},
            {"app_ids": {"ios": "ios01"}, "symbol_token": "fixture-web-only"},
            {"symbol_token": "fixture\ninvalid"},
        ):
            with self.subTest(values={k: v for k, v in values.items() if "token" not in k}):
                with self.assertRaises(ValueError):
                    self.write_config(**values)
                self.assertFalse((self.directory / "runner").exists())

    def test_setup_rejects_symlinks(self):
        target = self.directory / "target"
        target.mkdir()
        link = self.directory / "runner"
        link.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symbolic links"):
            self.write_config()
        self.assertEqual(list(target.iterdir()), [])

    def fake_launcher(self, action, *, fail_check=False):
        fakebin = self.directory / "bin"
        fakebin.mkdir(exist_ok=True)
        log = self.directory / "docker-args.jsonl"
        log.write_text("")
        docker = fakebin / "docker"
        docker.write_text(f"#!{sys.executable}\nimport os,sys,json\n"
                          "with open(os.environ['DOCKER_CALL_LOG'], 'a') as f: f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
                          "sys.exit(9 if os.environ.get('FAIL_CHECK') == '1' and 'run' in sys.argv else 0)\n")
        docker.chmod(0o755)
        env_file = self.directory / "inputs with spaces.env"
        sentinel = self.directory / "must-not-exist"
        env_file.write_text("UNUSED=$(touch " + str(sentinel) + ")\n")
        env = dict(os.environ, PATH=str(fakebin) + os.pathsep + os.environ.get("PATH", ""),
                   DOCKER_CALL_LOG=str(log), FAIL_CHECK="1" if fail_check else "0")
        result = subprocess.run(["bash", str(ROOT / "deploy/cloud/runner.sh"), "--env-file", str(env_file), action],
                                env=env, capture_output=True, text=True)
        self.assertFalse(sentinel.exists(), "The launcher must never source an env file as shell code")
        return result, [json.loads(line) for line in log.read_text().splitlines()]

    def test_failed_check_never_starts_or_stops_running_container(self):
        for action in ("start", "update"):
            result, calls = self.fake_launcher(action, fail_check=True)
            self.assertEqual(result.returncode, 9)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][-4:], ["run", "--rm", "--no-deps", "check"])

    def test_update_stops_old_container_before_starting_new_one(self):
        result, calls = self.fake_launcher("update")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0][-4:], ["run", "--rm", "--no-deps", "check"])
        self.assertEqual(calls[1][-2:], ["stop", "generator"])
        self.assertEqual(calls[2][-4:], ["up", "-d", "--no-deps", "generator"])

    def test_controls_call_existing_container_cli(self):
        for action in ("status", "pause", "stop", "resume"):
            result, calls = self.fake_launcher(action)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][-7:], ["exec", "-T", "generator", "python", "-m", "server.cloud", action])

    @unittest.skipUnless(shutil.which("docker"), "Docker CLI not installed")
    def test_compose_resolves_private_persistent_and_offline_services(self):
        env_file, _, _ = self.write_config(tenant="example", trace_token="fixture-local-only")
        env = {key: value for key, value in os.environ.items() if not key.startswith("SG_")}
        result = subprocess.run(["docker", "compose", "--env-file", str(env_file), "-f",
                                 str(ROOT / "docker-compose.cloud.yml"), "--profile", "check", "config", "--format", "json"],
                                capture_output=True, text=True, env=env)
        if "not a docker command" in result.stderr:
            self.skipTest("Docker Compose is not installed")
        self.assertEqual(result.returncode, 0, result.stderr)
        config = json.loads(result.stdout)
        runner = config["services"]["generator"]
        check = config["services"]["check"]
        self.assertEqual(runner["environment"]["SG_CLOUD_RUNNER"], "1")
        self.assertEqual(runner["environment"]["SG_REQUIRE_AUTH"], "1")
        self.assertEqual(runner["ports"][0]["host_ip"], "127.0.0.1")
        self.assertEqual(runner["restart"], "unless-stopped")
        self.assertEqual(runner["user"], "10001:10001")
        self.assertEqual(check["network_mode"], "none")
        self.assertEqual(check["environment"]["SG_CLOUD_RUNNER"], "0")
        self.assertNotIn("ports", check)
        self.assertEqual(check["command"][-2:], ["--sessions", "12"])
        self.assertTrue(all(volume["target"] != "/var/lib/session-generator" for volume in check["volumes"]))
        self.assertTrue(all(volume["read_only"] for volume in check["volumes"]))
        self.assertNotIn("fixture-local-only", result.stdout)


if __name__ == "__main__":
    unittest.main()
