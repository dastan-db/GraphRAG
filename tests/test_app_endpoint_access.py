#!/usr/bin/env python3
"""Validate that the Databricks App service principal can query serving endpoints.

This test checks the permission layer that sits between the deployed web app
and the model serving endpoints.  It catches the "You do not have permission
to query the endpoint" error before users see it.

Three checks per endpoint:
  1. Endpoint exists and is READY
  2. App service principal has CAN_QUERY permission
  3. (optional, --live) A lightweight invocation succeeds

Usage:
    python tests/test_app_endpoint_access.py              # permission checks only
    python tests/test_app_endpoint_access.py --live        # + live query test
    pytest tests/test_app_endpoint_access.py -v            # via pytest
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from databricks.sdk import WorkspaceClient

APP_NAME = os.getenv("DATABRICKS_APP_NAME", "graphrag-demo-v2-dev")

ENDPOINTS = [
    {"env": "GRAPHRAG_ENRON_ENDPOINT_NAME", "default": "graphrag-enron-agent"},
]


def _get_workspace_client() -> WorkspaceClient:
    return WorkspaceClient(profile="DEFAULT")


def _get_app_sp_application_id(w: WorkspaceClient, app_name: str) -> str:
    """Return the applicationId (client_id) of the Databricks App's service principal."""
    resp = w.api_client.do("GET", f"/api/2.0/apps/{app_name}")
    sp_id = resp.get("service_principal_client_id", "")
    if not sp_id:
        raise RuntimeError(f"App '{app_name}' has no service_principal_client_id")
    return sp_id


def _check_endpoint_ready(w: WorkspaceClient, endpoint_name: str) -> dict:
    ep = w.serving_endpoints.get(endpoint_name)
    state = ep.state
    ready = str(state.ready) if state else "UNKNOWN"
    return {
        "endpoint": endpoint_name,
        "id": ep.id,
        "ready": "READY" in ready,
        "state": ready,
    }


def _check_sp_permission(
    w: WorkspaceClient, endpoint_id: str, sp_application_id: str
) -> dict:
    """Check if the service principal has CAN_QUERY on the endpoint."""
    resp = w.api_client.do(
        "GET", f"/api/2.0/permissions/serving-endpoints/{endpoint_id}"
    )
    acls = resp.get("access_control_list", [])
    for acl in acls:
        if acl.get("service_principal_name") == sp_application_id:
            perms = [p.get("permission_level") for p in acl.get("all_permissions", [])]
            has_query = any(p in ("CAN_QUERY", "CAN_MANAGE") for p in perms)
            return {"has_permission": has_query, "permissions": perms}
    return {"has_permission": False, "permissions": []}


def _live_query(w: WorkspaceClient, endpoint_name: str) -> dict:
    """Send a minimal query to the endpoint and check for a non-error response."""
    try:
        resp = w.api_client.do(
            "POST",
            f"/serving-endpoints/{endpoint_name}/invocations",
            body={"input": [{"role": "user", "content": "ping"}]},
        )
        has_output = bool(resp.get("output"))
        return {"success": True, "has_output": has_output}
    except Exception as e:
        return {"success": False, "error": str(e)[:300]}


# ── pytest-compatible test functions ────────────────────────────────


def test_enron_endpoint_ready():
    w = _get_workspace_client()
    result = _check_endpoint_ready(w, "graphrag-enron-agent")
    assert result["ready"], f"Endpoint not ready: {result['state']}"


def test_enron_endpoint_permission():
    w = _get_workspace_client()
    sp_id = _get_app_sp_application_id(w, APP_NAME)
    ep_info = _check_endpoint_ready(w, "graphrag-enron-agent")
    perm = _check_sp_permission(w, ep_info["id"], sp_id)
    assert perm["has_permission"], (
        f"App SP {sp_id} lacks CAN_QUERY on graphrag-enron-agent. "
        f"Current permissions: {perm['permissions']}"
    )


# ── CLI runner ──────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Also send a test query")
    parser.add_argument("--app", default=APP_NAME, help="Databricks App name")
    args = parser.parse_args()

    w = _get_workspace_client()
    print(f"Workspace: {w.config.host}")

    sp_id = _get_app_sp_application_id(w, args.app)
    print(f"App: {args.app}")
    print(f"Service principal (applicationId): {sp_id}")

    all_passed = True

    for ep_cfg in ENDPOINTS:
        name = os.getenv(ep_cfg["env"], ep_cfg["default"])
        print(f"\n{'─' * 60}")
        print(f"  Endpoint: {name}")
        print(f"{'─' * 60}")

        try:
            info = _check_endpoint_ready(w, name)
        except Exception as e:
            print(f"  [FAIL] Endpoint lookup failed: {e}")
            all_passed = False
            continue

        status = "PASS" if info["ready"] else "FAIL"
        print(f"  [{status}] State: {info['state']}")
        if not info["ready"]:
            all_passed = False
            continue

        perm = _check_sp_permission(w, info["id"], sp_id)
        status = "PASS" if perm["has_permission"] else "FAIL"
        print(f"  [{status}] App SP CAN_QUERY: {perm['has_permission']}  (perms: {perm['permissions']})")
        if not perm["has_permission"]:
            all_passed = False
            print(f"         FIX: grant CAN_QUERY to application_id {sp_id} on endpoint {name}")

        if args.live:
            qr = _live_query(w, name)
            status = "PASS" if qr["success"] else "FAIL"
            print(f"  [{status}] Live query: {qr}")
            if not qr["success"]:
                all_passed = False

    print(f"\n{'=' * 60}")
    print(f"  {'ALL CHECKS PASSED' if all_passed else 'SOME CHECKS FAILED'}")
    print(f"{'=' * 60}")
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
