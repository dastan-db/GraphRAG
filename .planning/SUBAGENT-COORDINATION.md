# Subagent Coordination

How the 4 specialist agents work together within GSD phase execution.

## Project Ecosystem (from Task 0 Discovery)

All subagents operate inside the same Cursor workspace and inherit these shared resources:

### Project Rules (`.cursor/rules/`)
- `gsd-execution.mdc` — GSD execution framework (atomic tasks, phases, MECE verification, Working Backwards, XML plans, wave execution, commit conventions)

All subagents follow ALL project rules automatically. Subagent definitions add domain-specific instructions on top — they never override project-level rules.

### MCP Servers (`.cursor/mcp.json`)
- `databricks` — Databricks MCP server (Python subprocess via `.ai-dev-kit`, profile: DEFAULT). Provides Databricks CLI/SDK operations, SQL execution, Unity Catalog management, model serving, workspace file operations. Used by all 4 agents for their respective domain tasks.

### Skills (`.cursor/skills/`)
33 skills available, mapped to specialists by domain relevance:

| Skill | Data | Extraction | Developer | Quality |
|-------|:----:|:----------:|:---------:|:-------:|
| `databricks-dbsql` | ✓ | ✓ | | |
| `databricks-unity-catalog` | ✓ | | | ✓ |
| `databricks-python-sdk` | ✓ | ✓ | ✓ | |
| `databricks-config` | ✓ | | | |
| `databricks-synthetic-data-generation` | ✓ | | | |
| `databricks-spark-declarative-pipelines` | ✓ | | | |
| `databricks-model-serving` | | ✓ | ✓ | |
| `instrumenting-with-mlflow-tracing` | | ✓ | | ✓ |
| `agent-evaluation` | | ✓ | | ✓ |
| `databricks-app-python` | | | ✓ | |
| `databricks-asset-bundles` | | | ✓ | |
| `databricks-vector-search` | | | ✓ | |
| `databricks-agent-bricks` | | | ✓ | |
| `databricks-mlflow-evaluation` | | | | ✓ |
| `searching-mlflow-docs` | | | | ✓ |
| `querying-mlflow-metrics` | | | | ✓ |
| `retrieving-mlflow-traces` | | | | ✓ |
| `analyze-mlflow-trace` | | | | ✓ |

Additional skills exist (e.g., `databricks-docs`, `databricks-genie`, `databricks-jobs`, `databricks-lakebase-*`, `databricks-metric-views`, `databricks-spark-structured-streaming`, `databricks-zerobus-ingest`, `databricks-unstructured-pdf-generation`, `databricks-aibi-dashboards`, `databricks-app-apx`, `mlflow-onboarding`, `analyze-mlflow-chat-session`, `spark-python-data-source`, `gsd-workflow`) and are available to any agent if a task requires them.

### Existing Agent Definitions (`.cursor/agents/`)
None pre-existing — this implementation creates the first 4 agents.

## Specialists

| Agent | Domain | Writes To | Reads From |
|-------|--------|-----------|------------|
| Data Specialist | Data engineering, schema, Delta ops | `src/data/`, `data/`, `notebooks/data_*` | `.planning/`, config |
| Extraction Specialist | LLM extraction, dedup, knowledge graph | `src/extraction/`, `notebooks/extraction_*` | `src/data/` (read-only), `.planning/` |
| Developer Specialist | Agent, tools, API, UI | `src/agent/`, `src/app/`, `notebooks/agent_*`, `notebooks/demo_*` | `src/data/`, `src/extraction/` (read-only), `.planning/` |
| Quality & Learning | Verification, evaluation, metrics | `src/evaluation/`, `tests/`, `VERIFICATION.md`, `EVAL-REPORT.md` | Everything (read-only) |

## File Ownership Rules

**Rule 1:** Each file path has exactly ONE owning agent. No agent writes outside its owned paths.
**Rule 2:** Agents may READ any file but WRITE only to their owned paths.
**Rule 3:** If a task requires writing to another agent's path, the main agent must coordinate a sequential handoff (not parallel).
**Rule 4:** The Quality & Learning Specialist runs LAST — after all other agents have completed.

## Wave Structure (Per Phase)

```
Wave 1 (can be parallel): Data Specialist — data loading, schema work
    ↓ (data ready)
Wave 2 (foreground): Extraction Specialist — entity/relationship extraction
    ↓ (knowledge graph built)
Wave 3 (foreground): Developer Specialist — agent, tools, integration
    ↓ (deliverables complete)
Wave 4 (foreground): Quality & Learning Specialist — verify, evaluate, prepare learning data
    ↓ (metrics + observations ready)
Main Agent: Write Retrospective, update Decision Journal validation sections
```

## Parallel Execution Safety

- Waves 1–3 are sequential by dependency (each needs the previous wave's output)
- Within a wave, if multiple tasks are independent, they CAN run in parallel — but only if their file ownership doesn't overlap
- Wave 4 always runs after all other waves complete
- If a conflict is detected (two agents touched the same file), switch to sequential and document the pattern

## Handoff Protocol

When spawning a subagent, the main agent provides:
1. The specific task (from the phase plan)
2. Relevant requirements (from REQUIREMENTS.md)
3. The CONTRIBUTION-GATE.md for this phase
4. The previous phase's RETROSPECTIVE.md (if it exists)
5. Any relevant decision files from `.planning/decisions/`

When a subagent completes, it returns the structured output format defined in its agent definition. The main agent uses this output to:
- Assess whether the task succeeded
- Decide whether to proceed to the next wave or address blockers
- Accumulate data for the eventual Retrospective

## Adding New Specialists (v2+)

To add a new specialist:
1. Create `.cursor/agents/[name]-specialist.md` following the existing template pattern
2. Define its file ownership (must not overlap with existing agents)
3. Add it to this coordination document's table and wave structure
4. Specify where in the wave sequence it runs
