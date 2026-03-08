"""Poll the serving endpoint until config_update is done, then deploy v22."""
import sys
import time

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.serving import EndpointStateReady

ENDPOINT = "graphrag-bible-agent"
MODEL = "serverless_8e8gyh_catalog.graphrag_bible.graphrag_agent"
VERSION = sys.argv[1] if len(sys.argv) > 1 else "22"
MAX_WAIT = 900
POLL = 30

w = WorkspaceClient()

print(f"Waiting for endpoint '{ENDPOINT}' config update to finish...", flush=True)
elapsed = 0
while elapsed < MAX_WAIT:
    ep = w.serving_endpoints.get(name=ENDPOINT)
    ready = ep.state.ready if ep.state else None
    config_update = ep.state.config_update if ep.state else None
    print(f"  [{elapsed}s] ready={ready}, config_update={config_update}", flush=True)
    if ready == EndpointStateReady.READY and config_update is None:
        print("Config update complete!", flush=True)
        break
    time.sleep(POLL)
    elapsed += POLL
else:
    print(f"WARNING: Still updating after {MAX_WAIT}s", flush=True)
    sys.exit(1)

print(f"\nDeploying version {VERSION} to {ENDPOINT}...", flush=True)
from databricks import agents

deployment = agents.deploy(
    MODEL,
    VERSION,
    endpoint_name=ENDPOINT,
    tags={"source": "graphrag_solacc"},
)
print(f"Deployment initiated: {deployment.endpoint_name}", flush=True)

print("\nPolling until new deployment is READY...", flush=True)
elapsed = 0
while elapsed < MAX_WAIT:
    ep = w.serving_endpoints.get(name=ENDPOINT)
    ready = ep.state.ready if ep.state else None
    config_update = ep.state.config_update if ep.state else None
    print(f"  [{elapsed}s] ready={ready}, config_update={config_update}", flush=True)
    if ready == EndpointStateReady.READY and config_update is None:
        print(f"\nEndpoint '{ENDPOINT}' is READY with v{VERSION}!", flush=True)
        sys.exit(0)
    time.sleep(POLL)
    elapsed += POLL

print(f"WARNING: Did not reach READY within {MAX_WAIT}s", flush=True)
sys.exit(1)
