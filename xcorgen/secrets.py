"""Resolve secret references (token_ref) to values. Specs never contain secret values.

  keychain:<service>[/<account>]  macOS Keychain generic password (local Mac hosting)
  file:<absolute path>            owner-only token file (security.read_token_file rules)
  env:<VARIABLE>                  environment variable injected by a secret manager
"""
import os
import subprocess  # nosec B404: fixed argument vector, no shell

from .security import SecurityError, read_token_file


def resolve(token_ref):
    if not token_ref:
        return None
    scheme, _, rest = token_ref.partition(":")
    if scheme == "env":
        value = os.environ.get(rest)
        if not value:
            raise SecurityError(f"Environment variable {rest} is not set.")
        return value
    if scheme == "file":
        if not os.path.isabs(rest):
            raise SecurityError("file: token references need an absolute path.")
        return read_token_file(rest)
    if scheme == "keychain":
        service, _, account = rest.partition("/")
        args = ["/usr/bin/security", "find-generic-password", "-s", service]
        if account:
            args.extend(["-a", account])
        args.append("-w")
        try:
            out = subprocess.run(args, capture_output=True, text=True, timeout=10, check=True)  # nosec B603
        except (OSError, subprocess.SubprocessError):
            raise SecurityError(f"No Keychain item for service {service}.") from None
        return out.stdout.strip()
    raise SecurityError("Token references must start with keychain:, file: or env:.")
