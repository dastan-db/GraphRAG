# Subagent Coordination

How the 3 specialist agents work together within GSD phase execution.

## Project Ecosystem (from Task 0 Discovery)

All subagents operate inside the same Cursor workspace and inherit these shared resources:

### Project Rules (`.cursor/rules/`)
- `gsd-execution.mdc` — GSD execution framework (atomic tasks, phases, MECE verification, Working Backwards, XML plans, wave execution, commit conventions, fidelity levels, Drucker discipline)

All subagents follow ALL project rules automatically. Subagent definitions add specialist instructions on top — they never override project-level rules.

### MCP Servers (`.cursor/mcp.json`)
- `databricks` — Databricks MCP server (Python subprocess via `.ai-dev-kit`, profile: DEFAULT). Provides Databricks CLI/SDK operations, SQL execution, Unity Catalog management, model serving, workspace file operations. Used by all 3 agents for their respective domain tasks.

### Skills (`.cursor/skills/`)
33 skills available, mapped to specialists by domain relevance:

| Skill | Builder | Verifier | Evaluator |
|-------|:-------:|:--------:|:---------:|
| `databricks-dbsql` | ✓ | ✓ | |
| `databricks-python-sdk` | ✓ | ✓ | |
| `databricks-unity-catalog` | ✓ | ✓ | |
| `databricks-config` | ✓ | | |
| `databricks-spark-declarative-pipelines` | ✓ | | |
| `databricks-model-serving` | ✓ | | ✓ |
| `databricks-app-python` | ✓ | | |
| `databricks-asset-bundles` | ✓ | | |
| `databricks-vector-search` | ✓ | | |
| `databricks-agent-bricks` | ✓ | | |
| `instrumenting-with-mlflow-tracing` | ✓ | | ✓ |
| `agent-evaluation` | | ✓ | ✓ |
| `databricks-mlflow-evaluation` | | ✓ | ✓ |
| `searching-mlflow-docs` | | | ✓ |
| `querying-mlflow-metrics` | | | ✓ |
| `retrieving-mlflow-traces` | | | ✓ |
| `analyze-mlflow-trace` | | | ✓ |

Additional skills exist (e.g., `databricks-docs`, `databricks-genie`, `databricks-jobs`, `databricks-lakebase-*`, `databricks-metric-views`, `databricks-spark-structured-streaming`, `databricks-zerobus-ingest`, `databricks-unstructured-pdf-generation`, `databricks-aibi-dashboards`, `databricks-app-apx`, `mlflow-onboarding`, `analyze-mlflow-chat-session`, `spark-python-data-source`, `gsd-workflow`) and are available to any agent if a task requires them.

### Existing Agent Definitions (`.cursor/agents/`)
3 activity-based specialists: `builder.md`, `verifier.md`, `evaluator.md`.

## Specialists

| Agent | Purpose | Scoped To | File Ownership |
|-------|---------|-----------|----------------|
| Builder | Write code, create artifacts | Execution activity | `src/`, `notebooks/`, `RUNME.py` |
| Verifier | Test and validate | Quality activity | `tests/`, `VERIFICATION.md` |
| Evaluator | Measure and interpret | Learning activity | `src/evaluation/`, `EVAL-REPORT.md` |

## Why 3 Activity-Based Specialists (Not Pipeline Specialists)

Specialists are scoped around **recurring work patterns** (build, verify, evaluate) not pipeline stages (data, extraction, agent). This ensures:
- Every specialist is active in every phase — no idle capacity
- Domain knowledge comes from the phase plan, not baked into agent identity
- The wave structure is the same for every phase: Build → Verify → Evaluate
- Adding domain specialists later is easy if evidence shows the Builder is overloaded

## Fidelity Levels and Specialist Behavior

Each specialist adjusts its behavior based on the fidelity level specified in the phase plan:

| Fidelity | Builder Behavior | Verifier Behavior | Evaluator Behavior |
|----------|-----------------|-------------------|-------------------|
| Level 1 (Prove It) | Works in `notebooks/spikes/`. Hardcoded config OK. Speed priority. | Checks: does the spike work at all? | Quick qualitative assessment. Promote or abandon? |
| Level 2 (Shape It) | Extracts to `src/` modules. Clean interfaces, tests. | Full requirement validation, MECE audit. | MLflow evaluation, governance metrics. |
| Level 3 (Harden It) | Adds error handling, retry, observability. | Adversarial testing, failure mode validation. | Production baseline evaluation, drift detection. |

## Wave Structure (Per Phase)

```
Wave 1: Builder — executes all phase tasks at specified fidelity level
    ↓ (artifacts ready)
Wave 2: Verifier — tests and validates at appropriate depth for fidelity level
    ↓ (verification passed)
Wave 3: Evaluator — measures quality, prepares learning data
    ↓ (metrics + observations ready)
Main Agent: Write Retrospective, update Decision Journal, decide fidelity promotions
```

Three waves. Clean handoffs. Every specialist active in every phase.

## File Ownership Rules

**Rule 1:** Each file path has exactly ONE owning agent. No agent writes outside its owned paths.
**Rule 2:** Agents may READ any file but WRITE only to their owned paths.
**Rule 3:** The Verifier runs after the Builder completes. The Evaluator runs after the Verifier passes.
**Rule 4:** If the Builder needs to work at multiple fidelity levels (e.g., Level 1 spike + Level 2 promotion in the same phase), it handles both sequentially within Wave 1.

## Handoff Protocol

When spawning a subagent, the main agent provides:
1. The specific task (from the phase plan)
2. The target fidelity level (from the Contribution Gate)
3. Relevant requirements (from REQUIREMENTS.md)
4. The previous phase's RETROSPECTIVE.md (if it exists)
5. Any relevant decision files from `.planning/decisions/`

When a subagent completes, it returns the structured output format defined in its agent definition. The main agent uses this output to:
- Assess whether the task succeeded
- Decide whether to proceed to the next wave or address blockers
- Accumulate data for the eventual Retrospective

## Adding New Specialists (If Needed)

If the Builder becomes overloaded (too much domain context to hold), split it into domain specialists. Signals to watch for:
- Builder output quality degrades as domain complexity increases
- Phase plans consistently require >3 different kinds of code in the same wave
- Builder frequently needs to context-switch between radically different codebases

To add a new specialist:
1. Create `.cursor/agents/[name].md` following the existing template pattern
2. Define its file ownership (must not overlap with existing agents)
3. Add it to this coordination document
4. Specify where in the wave sequence it runs (likely parallel with existing Builder in Wave 1)
