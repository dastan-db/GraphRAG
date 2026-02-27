---
name: gsd-workflow
description: "GSD-style spec-driven workflow with Amazon Working Backwards (PRFAQ). Use when the user says 'new project', 'gsd new project', 'plan phase 1', 'execute phase 1', 'verify work 1', 'quick: ...', 'where am I', 'progress', 'discuss phase 1', 'complete milestone', 'new milestone', 'working backwards', 'prfaq', 'press release', 'start with the end in mind', 'write PRFAQ'. Follow .planning/ artifact layout and the gsd-execution rule."
---

# GSD workflow (Cursor)

Working Backwards + spec-driven flow: **PRFAQ** (end in mind) → **new-project** → per-phase **discuss** → **plan** → **execute** → **verify**; then **complete-milestone** / **new-milestone**. Same artifact layout and conventions as the gsd-execution rule.

## Triggers

Invoke this skill when the user says things like:

- "new project", "gsd new project", "initialize project"
- "working backwards", "prfaq", "write PRFAQ", "press release", "start with the end in mind"
- "discuss phase 1", "discuss phase N"
- "plan phase 1", "plan phase N"
- "execute phase 1", "execute phase N"
- "verify work 1", "verify work N"
- "quick: add dark mode", "quick: fix login button"
- "where am I", "progress", "what's next"
- "complete milestone", "new milestone"

## Workflow steps

### new-project

**Step 0 — Working Backwards (start with the end in mind):**

Before writing requirements or a roadmap, answer the five questions and write the PRFAQ:

1. Who is the customer? (time, place, situation)
2. What is the customer's problem or opportunity? (and size)
3. What is the most critical customer benefit?
4. How do we know what customers want or need? (data, not just intuition)
5. What does the customer experience look like? (journey, architecture, sketch)

Then write `.planning/PRFAQ.md` with three sections: **Five questions (answered)**, **Press release** (future-oriented, 1–2 pages, written as if shipped, focused on benefit and experience), **FAQ** (questions a customer or stakeholder would ask, including risks and trade-offs).

**Steps 1–7 (derive from PRFAQ):**

1. **Questions** (or load from an @-referenced doc): goals, constraints, tech preferences, edge cases — informed by the PRFAQ.
2. **Optional research:** Domain/stack research; store in `.planning/research/` if done.
3. **Requirements:** Write `.planning/REQUIREMENTS.md` with v1 / v2 / out of scope and requirement IDs. Requirements must be traceable to benefits in the PRFAQ.
4. **Roadmap:** Write `.planning/ROADMAP.md` with phases (table: phase number, name, status). Phases collectively must deliver the customer outcome in the PRFAQ.
5. **State:** Write `.planning/STATE.md` (position, decisions, blockers). Note PRFAQ status (e.g. "PRFAQ v1 done").
6. **Project:** Write `.planning/PROJECT.md` (vision, scope, constraints). Reference PRFAQ as "end in mind."
7. **Config (optional):** Create or update `.planning/config.json` (mode, depth, workflow toggles).

After this, the next step is **discuss-phase 1** then **plan-phase 1**.

---

### working-backwards (standalone)

Use when starting a new direction or after a pivot, without running the full new-project flow.

1. Answer the five questions for the new direction.
2. Write or update `.planning/PRFAQ.md` (full: five questions + press release + FAQ; or partial: press-release-only or FAQ-only update if the change is small).
3. Review REQUIREMENTS and ROADMAP for alignment; update them if the customer, problem, or experience has shifted.

---

### discuss-phase N

1. Resolve phase number N to the phase folder (e.g. `01` → `phases/01-setup` or the slug in ROADMAP).
2. Identify **gray areas** for that phase (e.g. UI layout, API shape, content structure, naming).
3. Ask the user until preferences are clear; then write `phases/XX-name/CONTEXT.md` with locked decisions.
4. This CONTEXT feeds **plan-phase** (research + planner) and keeps implementation aligned with the user's intent.

---

### plan-phase N

1. **Optional research:** If `workflow.research` is true and no `RESEARCH.md` exists, research the domain and write `phases/XX-name/RESEARCH.md`.
2. **Plan:** Write `XX-01-PLAN.md`, `XX-02-PLAN.md`, … in the phase folder. Each file must use the **XML task format** (see gsd-execution rule): `<task type="auto">` with `<name>`, `<files>`, `<action>`, `<verify>`, `<done>`. Keep to 2–3 tasks per phase.
3. **Plan check (optional):** If `workflow.plan_check` is true, verify that plans satisfy REQUIREMENTS and the phase goal; iterate until pass or user skips.
4. After this, the next step is **execute-phase N**.

---

### execute-phase N

1. **Discover plans:** List all `*-PLAN.md` in `phases/XX-name/`.
2. **Waves:** Infer dependencies; group into waves (independent plans in same wave, dependent in later waves).
3. **Run waves:** For each wave, run independent plans in parallel (e.g. one subagent per plan via mcp_task); run waves sequentially.
4. **Per task:** After each task, write the corresponding `*-SUMMARY.md` and **commit only that task** (atomic commit).
5. **Verifier (optional):** If `workflow.verifier` is true, check the codebase against phase goals and write `phases/XX-name/VERIFICATION.md`.
6. Update `STATE.md` and ROADMAP status as needed.

---

### verify-work N

1. **Deliverables:** From the phase and REQUIREMENTS, list testable deliverables.
2. **UAT:** Walk the user through each (e.g. "Can you do X?" — yes/no or describe issue).
3. **Failures:** If something is broken, optionally spawn a debug subagent to find root cause and produce fix plans (e.g. gap plans for re-execution).
4. Record results in the phase folder or STATE as appropriate.

---

### quick [description]

1. Create `.planning/quick/NNN-short-slug/` (NNN = next number, slug from description).
2. Write `PLAN.md` with **one** XML task (same format as phase plans).
3. Execute the task, write `SUMMARY.md`, then **one atomic commit**.

Use for: bug fixes, small features, config changes, one-off tasks.

---

### progress

1. Read `.planning/ROADMAP.md`, `.planning/STATE.md`, and phase folders.
2. Report: current milestone, current phase, what's done, **next step** (e.g. discuss-phase 2, plan-phase 1, execute-phase 1), and any blockers.

---

### complete-milestone

1. Confirm all phases for the milestone are verified.
2. Archive milestone (e.g. update or create `.planning/MILESTONES.md`), tag release if desired, update STATE for "next milestone".

---

### new-milestone [name]

1. If the new milestone changes the customer, problem, or core experience: run **working-backwards** first to update `.planning/PRFAQ.md`.
2. Same flow as **new-project** from Step 1 onward: questions/research → REQUIREMENTS (updated or new) → ROADMAP (new phases) → STATE.
3. Reuse existing PROJECT.md and codebase context; focus on what's being added or changed.

---

## File paths (must match gsd-execution rule)

| Artifact            | Path |
|---------------------|------|
| PRFAQ (end in mind) | `.planning/PRFAQ.md` |
| Project context     | `.planning/PROJECT.md` |
| Requirements        | `.planning/REQUIREMENTS.md` |
| Roadmap             | `.planning/ROADMAP.md` |
| State               | `.planning/STATE.md` |
| Config              | `.planning/config.json` |
| Phase context       | `.planning/phases/XX-slug/CONTEXT.md` |
| Phase research      | `.planning/phases/XX-slug/RESEARCH.md` |
| Phase plans         | `.planning/phases/XX-slug/XX-NN-PLAN.md` |
| Phase summaries     | `.planning/phases/XX-slug/XX-NN-SUMMARY.md` |
| Phase verify        | `.planning/phases/XX-slug/VERIFICATION.md` |
| Quick task          | `.planning/quick/NNN-slug/PLAN.md`, `SUMMARY.md` |

Always use the **XML task format** for plans (see gsd-execution rule). Respect `.planning/config.json` for workflow toggles (research, plan_check, verifier) when present.
