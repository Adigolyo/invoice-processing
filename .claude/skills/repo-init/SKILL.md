---
name: repo-init
description: Initialises this repository from its software factory project. Creates or updates README.md and CLAUDE.md from the project's discovery document, technical design and backlog. Use when asked to initialise, bootstrap or set up the repo, write its README or CLAUDE.md, or after the integration kit was just unzipped. Invoke explicitly with /repo-init.
---

# repo-init

Writes the repository's `README.md` and `CLAUDE.md` from what the software factory has
specified for this project. It merges into existing files and never discards content that
is already there.

## Step 0: Connect

1. Read `.software-factory/project.json` for `project_id` and `name`.
2. Check that the `software-factory` MCP server is connected. If it is not, stop and point
   the user to `INTEGRATION.md`.

Pass `project_id` to every `software-factory` tool call below.

## Step 1: Gather the facts

1. `list_artifacts`: see which documents exist.
2. `read_artifact` for the newest **discovery document** (from `epic-definer`): purpose,
   business context, the epics.
3. `read_artifact` for the newest **technical design(s)** (`technical-design-*.md` from
   `technical-designer`, RFC-style): tech stack,
   architecture, data model, repository layout, and how to build, run and test.
4. `read_backlog` without `epic_id`: use the returned `epics` manifest for the epic list.
   You don't need every story's text.
5. If the repository already has code, look at its manifests (`package.json`,
   `pyproject.toml`, `go.mod`, `Makefile`, …) for the real commands. The code wins over
   the RFC when they disagree. Say so in your report.

Record anything the documents don't state as unknown. Do not fill gaps with guesses.

## Step 2: README.md

It is for humans who are new to the project:

- **Title and one-paragraph purpose**, from the discovery document.
- **Features**: one line per epic (`EPIC-NNN: title`), from the manifest.
- **Architecture**: a short summary of the RFC's proposed architecture. Include a
  Mermaid diagram only if the RFC has one to base it on.
- **Tech stack**, from the RFC.
- **Getting started**: prerequisites, install, run, test. Use the commands from Step 1.
  Mark any the specification doesn't give as TODO.
- **Specification**: one line saying that stories, designs and plans live in the software
  factory project `<name>` and are read through the `software-factory` MCP server.

If `README.md` exists, keep its content. Update the sections above where they already
exist, add missing ones, and leave unrelated sections alone.

## Step 3: CLAUDE.md

It is for coding agents working in this repo:

1. The first line after the title must be `@.claude/software-factory.md`. That imports
   the always-on rules. Add it only if it isn't already present.
2. A **Project** section: the name and `project_id`, and one line of purpose.
3. A **Conventions** section:
   - Stack, from the RFC.
   - Directory layout, from the RFC or the repo.
   - Test command, lint/format command and run command, from Step 1.
   - Branching: say that tasks use the execution plan's source and target branches.
4. Keep every other section of an existing `CLAUDE.md` unchanged.

Keep `CLAUDE.md` short. Everything that already lives in the factory stays there and is
read through the MCP server; don't copy stories or designs into it.

## Step 4: Report

List the files you created or changed, the sections you touched, and every fact you
marked unknown or TODO. Don't commit. Leave that to the user.
