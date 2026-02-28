# GraphRAG Execution System

## How This Project Runs

Two disciplines, one accelerator, one maturity model:

**GSD (Execution):** Atomic tasks, XML plans, phase-based delivery, MECE verification. This is how we DO work. See `gsd-execution.mdc` for rules.

**Drucker Discipline (Learning):** Decision journals, phase retrospectives, contribution gates. This is how we LEARN from work. Templates in `.planning/templates/`.

**Cursor Subagents (Acceleration):** 3 specialist agents scoped around work patterns. This is how we PARALLELIZE work. Definitions in `.cursor/agents/`. Coordination in `.planning/SUBAGENT-COORDINATION.md`.

**Fidelity Levels (Maturity):** Three tiers that determine how code is written, where it lives, and when it gets promoted. This is how we manage the POC-to-production lifecycle.

## Fidelity Levels

| Level | Name | Where | When | Priority |
|-------|------|-------|------|----------|
| 1 | Prove It | `notebooks/spikes/` | Unproven approach | Speed |
| 2 | Shape It | `src/` modules | Proven, needs structure | Maintainability |
| 3 | Harden It | `src/` + `deploy/` | Validated, going to production | Reliability |

Promotion is explicit: spike notebook → `src/` module → hardened module. Never skip levels. Never let spikes silently become production code.

## Phase Lifecycle

```
1. Read Phase N-1 RETROSPECTIVE.md (learn from last phase)
2. Complete CONTRIBUTION-GATE.md (confirm alignment + target fidelity — 2 min)
3. Plan tasks (GSD: XML plans in phase folder)
4. Wave 1: Builder executes tasks at target fidelity
5. Wave 2: Verifier validates deliverables
6. Wave 3: Evaluator measures quality and prepares learning data
7. Main Agent: Write RETROSPECTIVE.md, decide fidelity promotions
8. Update decision files with validation evidence
9. Commit everything
10. Repeat
```

## File Structure

```
GraphRAG/                                # PROJECT ROOT
├── README.md                            [project overview]
├── RUNME.py                             [one-command demo — Databricks convention]
├── pyproject.toml                       [Python project config + dependencies]
├── databricks.yml                       [DABs deployment manifest]
├── .gitignore
│
├── src/                                 # ALL PRODUCT SOURCE CODE (Level 2+)
│   ├── config.py                        [shared config — catalog, schema, endpoints]
│   ├── data/                            [data engineering — loading, schema, Delta ops]
│   ├── extraction/                      [LLM extraction — prompts, pipeline, dedup]
│   ├── agent/                           [LangGraph agent — tools, state, serving]
│   ├── evaluation/                      [governance scorers, MLflow evaluation, baselines]
│   └── app/                             [Dash web application — pages, backend, assets]
│
├── notebooks/                           # DATABRICKS NOTEBOOKS
│   ├── 00_Intro_and_Config.py           [Level 2 walkthrough notebooks]
│   ├── 01_Data_Prep.py
│   ├── 02_Build_Knowledge_Graph.py
│   ├── 03_Build_Agent.py
│   ├── 04_Query_Demo.py
│   ├── 05_Evaluation.py
│   └── spikes/                          [Level 1 proving ground — temporary explorations]
│       └── 04_agent_spike.py            [temporary, phase-prefixed]
│
├── tests/                               # ALL TESTS (owned by Verifier)
│   ├── test_demo_app.py
│   ├── unit/
│   └── integration/
│
├── deploy/                              # DEPLOYMENT SCRIPTS + CONFIG
│   ├── pipeline_job.yml                 [DABs pipeline job definition]
│   └── webapp.yml                       [DABs web app definition]
│
├── docs/                                # DOCUMENTATION (non-planning)
├── data/                                # RAW/REFERENCE DATA (small, committed)
│
├── .cursor/                             # CURSOR TOOLING
│   ├── rules/
│   │   └── gsd-execution.mdc           [GSD + Drucker + Fidelity]
│   ├── mcp.json                         [MCP server config — shared by all agents]
│   ├── skills/                          [33 project skills — available to all agents]
│   └── agents/
│       ├── builder.md                   [Wave 1: writes code at target fidelity]
│       ├── verifier.md                  [Wave 2: tests and validates]
│       └── evaluator.md                 [Wave 3: measures and interprets]
│
└── .planning/                           # GSD + DRUCKER DISCIPLINE
    ├── SYSTEM.md                        ← you are here
    ├── PRFAQ.md                         [project vision — don't modify]
    ├── PROJECT.md                       [scope — don't modify]
    ├── REQUIREMENTS.md                  [deliverables]
    ├── ROADMAP.md                       [milestones]
    ├── STATE.md                         [current status]
    ├── SUBAGENT-COORDINATION.md         [wave structure + file ownership + ecosystem inventory]
    ├── decisions/
    │   ├── README.md                    [decision index]
    │   ├── D-001-delta-over-neo4j.md
    │   ├── D-002-bible-corpus.md
    │   ├── D-003-dash-framework.md
    │   └── D-004-debug-notebook-workflow.md
    ├── templates/
    │   ├── DECISION-TEMPLATE.md
    │   ├── RETROSPECTIVE-TEMPLATE.md
    │   └── CONTRIBUTION-GATE-TEMPLATE.md
    └── phases/
        ├── 01-setup/
        ├── 03-interactive-demo/
        │   └── RETROSPECTIVE.md         [Phase 03 — backfilled]
        ├── 04-live-agent/               [next phase]
        └── 05-evaluation/
```

## 3 Specialists

| Agent | What It Does | When | Adapts To |
|-------|-------------|------|-----------|
| Builder | Writes code, creates artifacts | Wave 1 | Fidelity level: spikes at L1, modules at L2, hardening at L3 |
| Verifier | Tests and validates | Wave 2 | Fidelity level: "does it work?" at L1, full MECE at L2, adversarial at L3 |
| Evaluator | Measures and interprets | Wave 3 | Fidelity level: qualitative at L1, metrics at L2, baselines at L3 |

The main agent writes the Retrospective, updates decisions, and decides fidelity promotions. Specialists execute; the main agent thinks.

## Rules That Matter

1. **Subagents inherit the full `.cursor/` ecosystem** — rules, MCPs, and skills apply to every agent automatically
2. **No phase starts without a completed Contribution Gate** (includes target fidelity level)
3. **No phase ends without a Retrospective** (includes Fidelity Status table)
4. **Every major decision gets a file** in `.planning/decisions/` (with reversibility + fidelity + platform leverage)
5. **One-way door + Level 1 fidelity = red flag** — promote the code or reconsider the decision
6. **Spike notebooks live in `notebooks/spikes/`** — max 2 phases before promotion or deletion
7. **Fidelity promotion is a planned task** — never a gradual drift from notebook to production
8. **Retrospective is the main agent's job** (not delegated to a subagent)
