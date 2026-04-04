import base64
import json

from src.agent.enron_promotion import ENRON_PIP_REQUIREMENTS, resolve_lakebase_username


def test_enron_serving_requirements_include_binary_psycopg():
    assert "psycopg[binary,pool]>=3.0" in ENRON_PIP_REQUIREMENTS


def _jwt_with_payload(payload: dict[str, str]) -> str:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).decode().rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{header}.{body}.signature"


def test_resolve_lakebase_username_prefers_token_subject():
    token = _jwt_with_payload({"sub": "serving-principal@databricks.com"})

    username = resolve_lakebase_username(
        token,
        workspace_user_name="3bb0df3e-b622-475f-a860-dcf9f758c0e2",
    )

    assert username == "serving-principal@databricks.com"


def test_resolve_lakebase_username_prefers_explicit_override(monkeypatch):
    token = _jwt_with_payload({"sub": "serving-principal@databricks.com"})
    monkeypatch.setenv("GRAPHRAG_LAKEBASE_USERNAME", "override@databricks.com")

    username = resolve_lakebase_username(
        token,
        workspace_user_name="3bb0df3e-b622-475f-a860-dcf9f758c0e2",
    )

    assert username == "override@databricks.com"
