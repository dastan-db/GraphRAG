# GraphRAG Execution System

## How This Project Runs

Two disciplines, one accelerator:

**GSD (Execution):** Atomic tasks, XML plans, phase-based delivery, MECE verification. This is how we DO work. See `gsd-execution.mdc` for rules.

**Drucker Discipline (Learning):** Decision journals, phase retrospectives, contribution gates. This is how we LEARN from work. Templates in `.planning/templates/`.

**Cursor Subagents (Acceleration):** 4 specialist agents with file ownership isolation. This is how we PARALLELIZE work. Definitions in `.cursor/agents/`. Coordination in `.planning/SUBAGENT-COORDINATION.md`.

## Phase Lifecycle

```
1. Read Phase N-1 RETROSPECTIVE.md (learn from last phase)
2. Complete CONTRIBUTION-GATE.md (confirm alignment — 2 min)
3. Plan tasks (GSD: XML plans in phase folder)
4. Execute tasks (spawn subagents per wave structure)
5. Run Quality & Learning Specialist (verification + evaluation)
6. Write RETROSPECTIVE.md (main agent — strategic reflection)
7. Update decision files with validation evidence
8. Commit everything
9. Repeat
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
├── src/                                 # ALL PRODUCT SOURCE CODE
│   ├── config.py                        [shared config — catalog, schema, endpoints]
│   ├── data/                            [data engineering — loading, schema, Delta ops]
│   ├── extraction/                      [LLM extraction — prompts, pipeline, dedup]
│   ├── agent/                           [LangGraph agent — tools, state, serving]
│   ├── evaluation/                      [governance scorers, MLflow evaluation, baselines]
│   └── app/                             [Dash web application — pages, backend, assets]
│
├── notebooks/                           # DATABRICKS NOTEBOOKS
│   ├── 00_Intro_and_Config.py           [pipeline walkthrough notebooks]
│   ├── 01_Data_Prep.py
│   ├── 02_Build_Knowledge_Graph.py
│   ├── 03_Build_Agent.py
│   ├── 04_Query_Demo.py
│   ├── 05_Evaluation.py
│   └── spikes/                          [Level 1 proving ground — temporary explorations]
│
├── tests/                               # ALL TESTS
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
│   │   └── gsd-execution.mdc           [GSD + Drucker discipline]
│   ├── mcp.json                         [MCP server config — shared by all agents]
│   ├── skills/                          [33 project skills — available to all agents]
│   └── agents/
│       ├── data-specialist.md
│       ├── extraction-specialist.md
│       ├── developer-specialist.md
│       └── quality-learning-specialist.md
│
└── .planning/                           # GSD + DRUCKER DISCIPLINE
    ├── SYSTEM.md                        ← you are here
    ├── PRFAQ.md                         [project vision — don't modify]
    ├── PROJECT.md                       [scope — don't modify]
    ├── REQUIREMENTS.md                  [deliverables]
    ├── ROADMAP.md                       [milestones]
    ├── STATE.md                         [current status]
    ├── SUBAGENT-COORDINATION.md         [wave structure + file ownership]
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
        └── ...
```

## 4 Specialists

| Agent | What It Does | When It Runs |
|-------|-------------|--------------|
| Data Specialist | Schema, Delta ops, data quality | Wave 1 |
| Extraction Specialist | LLM extraction, dedup, knowledge graph | Wave 2 |
| Developer Specialist | Agent, tools, API, UI | Wave 3 |
| Quality & Learning | Verify, evaluate, prepare learning data | Wave 4 (always last) |

The main agent writes the Retrospective and updates decisions. Specialists prepare data; the main agent makes judgments.

## Rules That Matter

1. **Subagents inherit the full `.cursor/` ecosystem** — rules, MCPs, and skills apply to every agent automatically
2. **No phase starts without a completed Contribution Gate** (2 min to fill out)
3. **No phase ends without a Retrospective** (captures learning for next phase)
4. **Every major decision gets a file** in `.planning/decisions/` (with reversibility + platform leverage check)
5. **Subagents only write to their owned paths** (see SUBAGENT-COORDINATION.md)
6. **Quality & Learning Specialist runs last** (after all other work is done)
7. **Retrospective is the main agent's job** (not delegated to a subagent)
