# ruff: noqa: INP001
"""Validate the A3 seccomp profile's socket predicates before running A4 probes.

OCI's ``SCMP_CMP_MASKED_EQ`` operands are ``value=mask`` and
``valueTwo=expected masked value``.  The check is deliberately explicit: a
profile that merely parses but reverses those operands can silently permit raw
sockets on one runtime and fail closed on another.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROFILE = Path(__file__).with_name("seccomp-profile.json")
SOCK_RAW = 3
SOCK_CLOEXEC = 0x80000
AF_PACKET = 17
SOCK_TYPE_MASK = 15


def main() -> int:
    """Validate the profile's domain and masked socket-type predicates."""
    profile = json.loads(PROFILE.read_text())
    rules = profile.get("syscalls", [])
    socket_rules = [rule for rule in rules if "socket" in rule.get("names", [])]
    failures: list[str] = []

    packet = next((rule for rule in socket_rules if rule.get("args", [{}])[0].get("index") == 0), None)
    if not packet or packet.get("args", [{}])[0].get("value") != AF_PACKET:
        failures.append("AF_PACKET (domain 17) deny rule is missing")

    raw = next((rule for rule in socket_rules if rule.get("args", [{}])[0].get("index") == 1), None)
    arg = raw.get("args", [{}])[0] if raw else {}
    if arg.get("op") != "SCMP_CMP_MASKED_EQ":
        failures.append("SOCK_RAW rule does not use SCMP_CMP_MASKED_EQ")
    elif arg.get("value") != SOCK_TYPE_MASK or arg.get("valueTwo") != SOCK_RAW:
        failures.append("SOCK_RAW operands must be value=15 mask, valueTwo=3")
    elif (SOCK_RAW & arg["value"]) != arg["valueTwo"] or ((SOCK_RAW | SOCK_CLOEXEC) & arg["value"]) != arg["valueTwo"]:
        failures.append("SOCK_RAW predicate does not match raw sockets with CLOEXEC")

    if failures:
        for failure in failures:
            sys.stdout.write(f"[FAIL] {failure}\n")
        return 1

    sys.stdout.write(f"[PASS] AF_PACKET domain predicate: {AF_PACKET} denied\n")
    sys.stdout.write(
        f"[PASS] SOCK_RAW type predicate: (type & {SOCK_TYPE_MASK}) == {SOCK_RAW}, including SOCK_CLOEXEC\n"
    )
    sys.stdout.write("[PASS] seccomp profile operands match OCI masked-equality semantics\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
