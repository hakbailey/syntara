#!/bin/sh
# A4 operator entry point.  The harness image intentionally has no usable shell;
# this wrapper is run from the host and asks Podman to execute the Python probes
# as the harness's non-root user.
set -eu

container=${AGENT_HARNESS_CONTAINER:-syntara_agent-harness_1}
exec podman exec --user 1001:0 "$container" python /opt/agent-sandbox/attack_suite.py "$@"
