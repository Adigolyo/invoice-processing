---
name: qa-review
description: Grades the current git diff against this project's acceptance criteria and QA strategy, read live from the software factory, producing a strict PASS/FAIL verdict per scenario. Use when asked to review, grade, verify or QA-check an implementation. Invoke explicitly with /qa-review [EPIC-NNN or task]. Read-only, never edits code.
---

# qa-review

Grades the changes against what the software factory specified. It is deliberately
independent of the run that produced the changes. Assume nothing is correct; grade only
what the diff and the specification actually show.

Pass `project_id` from `.software-factory/project.json` to every `software-factory` tool
call. If the MCP server isn't connected, stop and point the user to `INTEGRATION.md`.

## Step 1: Resolve scope

Work out the epic and, if possible, the task under review. Use, in order:

1. an ID the user gave;
2. the branch name (`task/EPIC-NNN-<n>-…`);
3. the commit messages on the branch.

Call `get_implementation_context(epic_id=…)`. From its `tasks` bucket, take the task's
row: **AC**, **Testing** and **Files**. Then take the stories it covers.

## Step 2: Get the diff

- Default: the branch against its target, `git diff <Target Branch>...HEAD`, plus
  uncommitted changes.
- If the user names a base, use that.
- If this isn't a git repository, stop and say so. Never grade without a diff.

## Step 3: Read the contract

For each covered story, call `get_implementation_context(story_id=…)`. It returns the
story's acceptance criteria verbatim, plus the QA strategy's BDD scenarios and the
design's contracts. When a supporting document is narrowed or truncated, call
`read_artifact` for the full QA strategy (`QA-STRATEGY-EPIC-NNN-*.md`) so you don't miss
any scenario.

## Step 4: Grade

Grade every item, and never skip one because the diff didn't touch it:

- each of the task's AC;
- each of the covered stories' acceptance criteria;
- each BDD scenario for those stories.

Verdicts:

- **PASS**: the diff visibly implements and tests it, with evidence.
- **FAIL**: the diff contradicts or breaks it.
- **NOT-COVERED**: the diff doesn't address it. This counts as FAIL.

Then check the blast radius with the code-graph tools:

- `trace_path` from each changed function to its callers;
- `detect_changes`, where available, for affected symbols.

Flag callers the diff breaks, and changed code near a QA-strategy **Architectural Risk**
(`R-NN`) with no visible mitigation. These are notes, not verdict rows.

Also flag changes outside the task's **Files** as scope notes.

## Step 5: Output

Produce exactly this:

```
| Item | Verdict | Evidence | Notes |
|---|---|---|---|
| Task AC1 | PASS | src/orders/service.py:42 implements X; tests/test_orders.py:10 | |
| USR-003-02 AC2 | NOT-COVERED | not found in diff | |
| Scenario: [Negative Path] … | FAIL | … | |

**Summary:** X/Y passed

**Blast radius / risks / scope:** …

## Overall Verdict: PASS | FAIL
```

The overall verdict is PASS only if every row is PASS.

## Rules

- Read-only. Never edit code, tests or specifications from this skill.
- Every verdict cites concrete `file:line` evidence.
