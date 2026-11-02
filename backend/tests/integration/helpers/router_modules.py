"""Single source of truth for router modules that bind get_authz_evaluator.

The integration conftest imports these modules at collection time (before any
fixture or monkeypatch can run) so their Depends(...) captures always bind the
real function; the router-binding regression test imports the same tuple to
verify every module in it. Keep this list in sync with the routers that use
get_authz_evaluator, directly or via Depends.
"""

ROUTER_MODULES: tuple[str, ...] = (
    "syntara.aap.router",
    "syntara.approvals.router",
    "syntara.auth.router",
    "syntara.authz.role_assignment_router",
    "syntara.authz.router",
    "syntara.credentials.router",
    "syntara.workflows.executions_router",
)
