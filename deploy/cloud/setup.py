#!/usr/bin/env python3
"""One-time Linux host setup; secrets are entered privately, never in argv or .env."""
import argparse
import getpass
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile


def validate_inputs(image, app_ids, tenant):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/@:-]*", image):
        raise ValueError("Enter a container image name with a versioned tag or digest.")
    if ":" not in image.rsplit("/", 1)[-1] or image.endswith(":latest"):
        raise ValueError("Use an immutable version tag or sha256 digest, not an unversioned/latest image.")
    selected = [value for value in app_ids.values() if value]
    if not selected:
        raise ValueError("Enter at least one app ID.")
    if any(not re.fullmatch(r"[A-Za-z0-9]{5}", value) for value in selected):
        raise ValueError("App IDs must contain five letters or digits.")
    if len({value.lower() for value in selected}) != len(selected):
        raise ValueError("Each platform requires its own different app ID.")
    if any(value.lower() == "ios00" for value in selected):
        raise ValueError("Use your real iOS app ID, not the repository's ios00 placeholder.")
    if tenant and not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]*", tenant):
        raise ValueError("Enter the Chronosphere tenant name or hostname, without https:// or a path.")


def private_directory(path, uid):
    if path.is_symlink():
        raise ValueError("Setup paths cannot be symbolic links.")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if not path.is_dir():
        raise ValueError("Setup path must be a directory.")
    os.chmod(path, 0o700)
    os.chown(path, uid, uid)


def atomic_private_file(path, value, uid):
    if path.is_symlink():
        raise ValueError("Setup files cannot be symbolic links.")
    fd, temporary = tempfile.mkstemp(prefix=".setup-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        os.fchown(fd, uid, uid)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = None
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if fd is not None:
            os.close(fd)
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_setup(directory, image, app_ids, tenant, trace_token, symbol_token, *, replace=False, uid=10001):
    validate_inputs(image, app_ids, tenant)
    if bool(tenant) != bool(trace_token):
        raise ValueError("A backend tenant and its API token must be supplied together.")
    if symbol_token and not app_ids.get("web"):
        raise ValueError("A Symbol Upload key applies only to the web app.")
    for token in (trace_token, symbol_token):
        if token and (len(token) > 8192 or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in token)):
            raise ValueError("A credential contains unsupported characters or is too long.")
    directory = Path(directory).absolute()
    # Values become a Compose env file, not executable shell syntax.
    if not re.fullmatch(r"/[A-Za-z0-9/_.-]+", str(directory)) or ".." in directory.parts:
        raise ValueError("Use an absolute setup directory containing letters, digits, /, _, . or -.")
    if any(parent.is_symlink() for parent in (directory, *directory.parents)):
        raise ValueError("Setup paths cannot traverse symbolic links.")
    env_file = directory / ".env"
    if env_file.exists() and not replace:
        raise ValueError("Configuration already exists. Use --replace to prepare updated inputs.")
    private_directory(directory, os.getuid())
    private_directory(directory / "data", uid)
    private_directory(directory / "credentials", uid)
    values = {
        "SG_IMAGE": image,
        "SG_HOST_DATA_DIR": str(directory / "data"),
        "SG_HOST_SECRETS_DIR": str(directory / "credentials"),
        **{"SG_" + platform.upper() + "_APP_ID": app_ids.get(platform, "") for platform in ("web", "android", "ios")},
        "SG_CHRONOSPHERE_TENANT": tenant,
    }
    # New versions never overwrite a key file potentially used by an active run.
    for name, token in (("CHRONOSPHERE", trace_token), ("SYMBOL_UPLOAD", symbol_token)):
        if token:
            filename = name.lower() + "-" + secrets.token_hex(8) + ".token"
            atomic_private_file(directory / "credentials" / filename, token, uid)
            values["SG_" + name + "_TOKEN_FILE"] = "/run/secrets/" + filename
        else:
            values["SG_" + name + "_TOKEN_FILE"] = ""
    atomic_private_file(env_file, "# Non-secret inputs; credentials are separate protected files.\n" +
                        "".join(f"{key}={value}\n" for key, value in values.items()), os.getuid())
    return env_file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", default="/srv/session-generator")
    parser.add_argument("--replace", action="store_true", help="prepare updated inputs without starting/stopping the runner")
    args = parser.parse_args()
    if sys.platform != "linux" or os.geteuid() != 0:
        parser.error("Run this on the Linux runner host with sudo so it can assign UID 10001 to the mounts.")
    if not sys.stdin.isatty():
        parser.error("Interactive setup requires a terminal; use your secret manager to provision protected mounts for automation.")
    print("Supply the apps to generate. Leave an app ID blank to exclude its platform.")
    try:
        image = input("Versioned container image: ").strip()
        app_ids = {platform: input(label + " app ID (optional): ").strip()
                   for platform, label in (("web", "Web"), ("android", "Android"), ("ios", "iOS"))}
        tenant = input("Chronosphere tenant (blank for sessions only): ").strip()
        validate_inputs(image, app_ids, tenant)
        trace_token = getpass.getpass("Chronosphere API token (hidden): ") if tenant else ""
        symbol_token = getpass.getpass("Web Symbol Upload key (optional, hidden): ") if app_ids["web"] else ""
        env_file = write_setup(args.directory, image, app_ids, tenant, trace_token, symbol_token, replace=args.replace)
    except (ValueError, OSError) as error:
        # Our value errors contain no inputs or credential values. Do not print OS exception bodies.
        print(str(error) if isinstance(error, ValueError) else "Could not write protected runner configuration.", file=sys.stderr)
        return 2
    except (EOFError, KeyboardInterrupt):
        print("\nSetup cancelled; the runner was not started.", file=sys.stderr)
        return 2
    print(f"Configuration prepared: {env_file}")
    print("Setup does not send traffic. Run deploy/cloud/runner.sh check, then start when ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
