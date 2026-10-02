## Software Factory

This repository implements a project specified by the software factory's SDLC pipeline.
The specification lives **in the factory, not in this repo**. Read it live through the
`software-factory` MCP server, so you always work from the current version.

**Project:** `.software-factory/project.json` holds the `project_id`. Pass it as
`project_id` to every `software-factory` tool call.

### Where things are

| You need | Call |
|---|---|
| Everything bearing on one story or epic (spec, design, tests, tasks, UI, infra) | `get_implementation_context(story_id=… \| epic_id=…)` |
| Every story, complete and verbatim | `read_backlog` (paged: follow `next_offset`) |
| A list of the pipeline's documents, or one in full | `list_artifacts`, `read_artifact` |
| One fact, when you don't know which document holds it | `search_knowledge` (passages only, so never use it for completeness) |
| The existing code: structure, symbols, callers, snippets | the code-graph tools: see the `code-context` skill |

The pipeline's documents are: the discovery document (epics, business context), user
stories (`USR-NNN-NN`, with BDD acceptance criteria), RFCs (architecture, data model, API
contracts, ADRs), QA strategies (BDD test scenarios), execution plans (PR-sized tasks,
from the Task Planner), the UI design (`UI-DESIGN-*.md` + `UI-TOKENS-*.json`), and
infrastructure blueprints.

### Always-on rules

1. Never assume the tech stack, architecture, data model or API contracts. Read them
   from the RFC and the story before writing code.
2. Work is planned in the factory. To build something, use `implement-task`, which runs
   one task from the Task Planner's execution plan test-first. If an epic has no execution
   plan, ask the user to run the Task Planner. Do not break the work down yourself.
3. After implementing, or whenever asked to review, use `qa-review`. Do not self-grade.
4. For questions about the existing code, use the code-graph tools (`code-context`)
   before reading files one by one.
5. When a UI design exists, build the frontend from it: the tokens are the theme, the
   spec is the screen layout. To draw it in Pencil or Figma, use `ui-render`.
6. If the specification is silent on something you need, say so and ask. Do not invent
   requirements.
7. If the `software-factory` MCP server is not connected, stop and tell the user to
   connect it (see `INTEGRATION.md`). Do not guess the specification.
