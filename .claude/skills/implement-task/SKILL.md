---
name: implement-task
description: Implements one task from this project's execution plan, the PR-sized breakdown the software factory's Task Planner produced, using test-driven development. Use when asked to implement, build or start work on a task, a story or an epic, or when the user mentions an EPIC-, USR- or task ID and wants code written. Invoke explicitly with /implement-task <EPIC-NNN> [task number].
---

# implement-task

Implements **one task** from the Task Planner's execution plan, test-first. The plan is
already made in the software factory: which tasks exist, their files, acceptance
criteria, dependencies and branches. This skill executes it. It never re-plans the work
and never invents technology choices, contracts or data models.

Pass `project_id` from `.software-factory/project.json` to every `software-factory` tool
call. If the MCP server isn't connected, stop and point the user to `INTEGRATION.md`.

## Step 0: Resolve the plan

1. Resolve the epic. Use the `EPIC-NNN` the user gave. If they named a story
   (`USR-NNN-NN`), the epic is `EPIC-NNN`. If they gave nothing, call `read_backlog` and
   ask which epic.
2. Call `get_implementation_context(epic_id="EPIC-NNN")`. Its `tasks` bucket holds the
   execution plan (`execution-plan-*.md`). If the plan is truncated or narrowed to
   passages (see `notes`), call `read_artifact` for the full file named in `sources`.
3. **No execution plan for the epic:** stop. Tell the user to run the Task Planner for
   this epic in the software factory, then come back. Do not break the work down
   yourself.

## Step 1: Pick the task

The plan is a table. Each row has a task number and title, a description with
**Files**, **AC** and **Testing**, **Requires** / **Required By**, a **Source Branch**,
a **Target Branch** and an estimated effort.

- If the user named a task, take it.
- Otherwise, list the tasks and propose the first one whose **Requires** tasks are done.
  A task counts as done when its branch is merged into its target, or its commits are in
  `git log`.
- If the chosen task depends on one that isn't done, say which, and continue only if the
  user confirms.

A task covers one or more stories. Name them (`USR-NNN-NN`) before going on.

## Step 2: Orient

Read, only as far as the task needs:

1. `get_implementation_context(story_id=…)` for each story the task covers. This gives the
   story verbatim (the specification, including its acceptance criteria), plus the design
   decisions and the QA strategy's BDD scenarios that bear on it.
2. The technical design's sections for the task's components: data model, API contracts,
   ADRs. If a passage is not enough, use `read_artifact` for the whole document.
3. When the task touches the UI: the `UI-DESIGN-*.md` screens involved and the
   `UI-TOKENS-*.json` theme (`agent="ui-designer"`).
4. The existing code, through the code-graph tools (see `code-context`):
   - `get_architecture` once, for the layout;
   - `search_graph` / `get_code_snippet` for the task's **Files** and their neighbours;
   - `trace_path` for callers of anything you will change;
   - the test layout and conventions for the tests you will write.

   The graph reflects the last index. For files changed locally since, read the file.
5. The conventions in `CLAUDE.md`: test, lint and run commands.

## Step 3: Branch

Check out the task's **Source Branch** (pull it if it exists remotely) and create the
task's branch from it, e.g. `task/EPIC-NNN-<n>-<short-title>`. If the working tree has
uncommitted changes, stop and ask first.

## Step 4: Red

Write the failing tests first, derived from:

- the task's **AC** and **Testing** notes;
- the covered stories' acceptance criteria;
- the QA strategy's BDD scenarios for those stories, including the negative and edge
  paths.

Follow the project's existing test conventions. Run the tests and confirm they fail for
the right reason.

## Step 5: Green, then refactor

Implement exactly enough to pass, in the task's **Files**:

- Follow the technical design's contracts and the story's constraints and out-of-scope
  list.
- If the task needs a file the plan didn't list, or a contract the design didn't define,
  stop and say so. Do not silently widen the scope.

Iterate until the suite is green. Refactor, then run the full test suite and the linter.

## Step 6: Hand off

1. Commit in two conventional commits:
   `test(EPIC-NNN): failing tests for task <n>`, then
   `feat(EPIC-NNN): <task title>`.
2. Don't push or open a PR unless asked. Suggest
   `gh pr create --base <Target Branch>`, with a body that lists the task, the stories and
   the ACs covered.
3. Report:
   - the files touched;
   - which ACs and BDD scenarios are now covered;
   - every assumption you made where the specification was silent;
   - the next unblocked task.
4. Recommend running `/qa-review`. Don't grade your own work.

## Rules

- One task per run. Don't implement other tasks "while you're there".
- Tests before implementation, always.
- The factory's documents are the specification. If they are wrong or incomplete, say so
  and suggest updating them in the factory. Don't work around them in code.
