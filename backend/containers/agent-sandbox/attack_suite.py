# ruff: noqa: INP001
"""Run the prototype A4 containment probes from inside the harness container.

The probes intentionally use only the Python standard library so this file can be
bind-mounted into the read-only harness image.  A zero exit status means every
probe was blocked by the container boundary.  This is an operator check, not a
replacement for the unit tests or a production security scanner.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path


def _probe(label: str, *, blocked: bool, detail: str) -> bool:
    status = "PASS" if blocked else "FAIL"
    sys.stdout.write(f"[{status}] {label}: {detail}\n")
    return blocked


def _http_escape() -> bool:
    try:
        with urllib.request.urlopen("http://example.com", timeout=2):
            return _probe("non-gate HTTP egress", blocked=False, detail="request unexpectedly succeeded")
    except Exception as exc:  # noqa: BLE001 - any denial is the expected result
        return _probe("non-gate HTTP egress", blocked=True, detail=type(exc).__name__)


def _raw_socket(domain: int, socket_type: int, label: str) -> bool:
    try:
        sock = socket.socket(domain, socket_type)
    except OSError as exc:
        return _probe(label, blocked=True, detail=f"blocked ({exc.__class__.__name__})")
    else:
        sock.close()
        return _probe(label, blocked=False, detail="socket unexpectedly opened")


def _shell_escape() -> bool:
    try:
        subprocess.run(["/bin/sh", "-c", "id"], check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        return _probe("disallowed shell execution", blocked=True, detail=f"blocked ({exc.__class__.__name__})")
    return _probe("disallowed shell execution", blocked=False, detail="shell unexpectedly executed")


def _secret_file() -> bool:
    candidates = (
        "/run/secrets/admin-password",
        "/run/secrets/s2s-cert.key",
        "/root/.env",
        "/app/.env",
    )
    readable = [path for path in candidates if Path(path).is_file() and os.access(path, os.R_OK)]
    return _probe(
        "credential/secret file access",
        blocked=not readable,
        detail="all scoped secret paths absent or unreadable" if not readable else f"readable: {readable}",
    )


def main() -> int:
    """Run all containment probes and return a shell-friendly status code."""
    checks = [
        _http_escape(),
        _raw_socket(socket.AF_PACKET, socket.SOCK_RAW, "AF_PACKET raw socket"),
        _raw_socket(socket.AF_INET, socket.SOCK_RAW, "AF_INET SOCK_RAW socket"),
        _shell_escape(),
        _secret_file(),
    ]
    sys.stdout.write(f"{sum(checks)}/{len(checks)} containment probes blocked\n")
    return 0 if all(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
