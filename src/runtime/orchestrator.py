from __future__ import annotations

import importlib
import os
from contextlib import contextmanager
from threading import RLock
from types import SimpleNamespace
from typing import Any

from databricks.sdk import WorkspaceClient

from .config import RuntimeConfig
from .contracts import RuntimeQuery, RuntimeTransport
from .responses import ParsedRuntimeResponse, parse_agent_response

_MODULE_LOCK = RLock()
_AGENT_MODULE = None
_AGENT_SIGNATURE: tuple[str, ...] | None = None


@contextmanager
def _temporary_env(updates: dict[str, str]):
    prior = {key: os.environ.get(key) for key in updates}
    os.environ.update(updates)
    try:
        yield
    finally:
        for key, old_value in prior.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value


class SharedRuntimeOrchestrator:
    """Shared runtime entrypoint for local scripts and the Databricks app backend.

    The orchestrator makes the app/backend and local tooling use the same in-process
    runtime by default, while preserving an endpoint transport as a compatibility mode.
    """

    def __init__(self, config: RuntimeConfig | None = None):
        self.config = config or RuntimeConfig.from_env()

    def modules(self):
        transport = SimpleNamespace(value="local")
        topology = SimpleNamespace(
            router=SimpleNamespace(transport=transport),
            planner=SimpleNamespace(transport=transport),
            graph=SimpleNamespace(transport=transport),
            evidence=SimpleNamespace(transport=transport),
            analytics=SimpleNamespace(transport=transport),
        )
        return SimpleNamespace(topology=topology)

    def query(self, runtime_query: RuntimeQuery) -> ParsedRuntimeResponse:
        if self.config.transport == RuntimeTransport.ENDPOINT:
            return self._query_endpoint(runtime_query)
        return self._query_direct(runtime_query)

    def _runtime_signature(self, runtime_query: RuntimeQuery) -> tuple[str, ...]:
        return (
            runtime_query.corpus,
            self.config.data_backend.value,
            self.config.llm_provider,
        )

    def _load_agent_module(self, runtime_query: RuntimeQuery):
        global _AGENT_MODULE, _AGENT_SIGNATURE

        signature = self._runtime_signature(runtime_query)
        env_updates = self.config.agent_environment(corpus=runtime_query.corpus)

        with _MODULE_LOCK:
            with _temporary_env(env_updates):
                import src.agent.agent_serving as agent_module

                if _AGENT_MODULE is None or _AGENT_SIGNATURE != signature:
                    agent_module = importlib.reload(agent_module)
                    _AGENT_MODULE = agent_module
                    _AGENT_SIGNATURE = signature
        return _AGENT_MODULE

    def _build_request(self, runtime_query: RuntimeQuery):
        from mlflow.types.responses import ResponsesAgentRequest

        messages = list(runtime_query.conversation)
        messages.append({"role": "user", "content": runtime_query.question})
        payload: dict[str, Any] = {"input": messages}

        custom_inputs: dict[str, str] = {}
        if runtime_query.user_tier:
            custom_inputs["user_tier"] = runtime_query.user_tier
        if runtime_query.permitted_books:
            custom_inputs["permitted_books"] = ",".join(runtime_query.permitted_books)
        if custom_inputs:
            payload["custom_inputs"] = custom_inputs

        return ResponsesAgentRequest.model_validate(payload)

    def _query_direct(self, runtime_query: RuntimeQuery) -> ParsedRuntimeResponse:
        agent_module = self._load_agent_module(runtime_query)
        request = self._build_request(runtime_query)
        agent = agent_module.GraphRAGAgent()
        response = agent.predict(request)
        return parse_agent_response(response)

    def _query_endpoint(self, runtime_query: RuntimeQuery) -> ParsedRuntimeResponse:
        endpoint = runtime_query.endpoint_name
        if not endpoint:
            endpoint = os.environ.get("GRAPHRAG_ENRON_ENDPOINT_NAME", "graphrag-enron-agent")

        messages = list(runtime_query.conversation)
        messages.append({"role": "user", "content": runtime_query.question})
        body: dict[str, Any] = {"input": messages}

        custom_inputs: dict[str, str] = {}
        if runtime_query.user_tier:
            custom_inputs["user_tier"] = runtime_query.user_tier
        if runtime_query.permitted_books:
            custom_inputs["permitted_books"] = ",".join(runtime_query.permitted_books)
        if custom_inputs:
            body["custom_inputs"] = custom_inputs

        client = WorkspaceClient()
        response = client.api_client.do(
            "POST",
            f"/serving-endpoints/{endpoint}/invocations",
            body=body,
        )
        return parse_agent_response(response)
