# Software Factory integration: Invoice processing

This kit connects Claude Code or Cursor in this repository to the software factory
project **Invoice processing** (`44eed337-e80e-4ca8-b6e2-eb1814b72816`). Your coding agent reads the project's
specification, designs, test strategy, task plan and code graph live from the factory's
MCP server. Nothing here is a copy of the specification, so nothing here goes stale.

## 1. Unzip into the repository root

Unzip the kit into the root of your repository. It adds only these files and overwrites
none of yours:

| Path | Purpose |
|---|---|
| `.mcp.json` | Claude Code: registers the `software-factory` MCP server |
| `.cursor/mcp.json` | Cursor: registers the same server |
| `.software-factory/project.json` | Which factory project this repo implements |
| `.claude/software-factory.md` | Always-on rules, imported into `CLAUDE.md` by `/repo-init` |
| `.claude/skills/*/SKILL.md` | The Claude Code skills below |
| `.cursor/rules/*.mdc` | The same skills as Cursor rules |

Commit them. Everyone who clones the repo gets the same setup.

## 2. Connect the MCP server

The server is at **http://108.131.214.204:20011/mcp**. Your machine must be able to reach it. If it runs
somewhere else, change the URL in `.mcp.json`, `.cursor/mcp.json` and
`.software-factory/project.json`.

**Claude Code:** start `claude` in the repository and approve the project MCP server when
asked. Then check that it is connected:

```bash
claude mcp list        # software-factory ... ✓ Connected
```

As an alternative to `.mcp.json`, register it for yourself only:

```bash
claude mcp add --transport http software-factory http://108.131.214.204:20011/mcp
```

**Cursor:** open *Settings → MCP*, then enable `software-factory`.

## 3. Initialise the repository

Run `/repo-init` in Claude Code, or ask Cursor to "initialise the repo from the software
factory". It writes `README.md` and `CLAUDE.md` from the project's discovery document and
technical design. If those files already exist, it merges into them and keeps your content.

## Skills

| Skill | Use it to |
|---|---|
| `/repo-init` | Create or update `README.md` and `CLAUDE.md` from the specification |
| `/implement-task EPIC-NNN [task]` | Implement one task from the Task Planner's execution plan, test-first |
| `/qa-review` | Grade the current diff against the acceptance criteria and BDD scenarios |
| `/code-context` | Answer questions about the existing code from the factory's code graph |
| `/ui-render [screen or epic]` | Draw the UI Designer's screens in Pencil or Figma |

The code graph covers the repositories attached to the project in the factory workspace
(*Repositories*). It reflects their last index, not your uncommitted changes.

`/ui-render` needs a design tool connected in the same session. For Pencil, install and
open the Pencil app or editor extension; its MCP server is available while it runs.

## Troubleshooting

- **`software-factory` not connected:** check that `http://108.131.214.204:20011/mcp` is reachable from your
  machine. In a browser, it answers with an error page rather than a timeout.
- **"No project selected":** the agent did not pass `project_id`. It is
  `44eed337-e80e-4ca8-b6e2-eb1814b72816`, recorded in `.software-factory/project.json`.
- **Empty code-graph results:** attach the repository to the project in the factory
  workspace, then wait for indexing to finish (`index_status`).
