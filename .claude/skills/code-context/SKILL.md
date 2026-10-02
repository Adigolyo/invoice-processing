---
name: code-context
description: How to answer questions about this project's existing code efficiently with the software factory's code graph (search_graph, trace_path, get_code_snippet, get_architecture, query_graph and related tools on the software-factory MCP server). Use when exploring or explaining the codebase, finding a symbol, its callers or its dependencies, judging the impact of a change, or before editing unfamiliar code. Invoke explicitly with /code-context <question>.
---

# code-context

The software factory keeps a code graph of every repository attached to this project:
functions, classes, modules, and the calls, imports and inheritance between them. The
graph tools on the `software-factory` MCP server query it. They answer structural
questions in one call where reading files would take dozens, and they cost far less
context.

Pass `project_id` from `.software-factory/project.json` to every call. The server scopes
each call to this project's graph.

## What the graph is and isn't

- It is built from the **last index** of the attached repositories (their default branch,
  or as attached in the workspace). It does **not** see your uncommitted or unpushed
  changes. For anything you changed locally, read the file itself.
- It covers only repositories attached to the project in the factory workspace
  (*Repositories*). If a repo is missing, the answer is "unknown", not "doesn't exist".
- When a tool reports that the service is unavailable, the code is *unknown*. Say so, and
  fall back to reading local files. Never conclude that the code doesn't exist.

## The routine

1. **Orient once:** `index_status`, to see which repos are indexed and how fresh they are.
   Then `get_architecture`, for languages, packages, entry points, hotspots and layers.
   Do this once per session, not per question.
2. **Find:** `search_graph` for symbols. Use `query`, or `name_pattern` / `qn_pattern`, or
   `semantic_query` to find a concept by meaning. Filter with `label` (`Function`, `Class`,
   `Method`, `Module`) and `file_pattern` to keep results small. Each hit carries the
   qualified name the other tools take.
3. **Relate:** `trace_path(function_name=…)` for callers and callees (`direction` inbound for
   "who calls this", outbound for "what does this call"). Keep `depth` low (1–2) and widen
   only if needed.
4. **Read:** `get_code_snippet(qualified_name=…)` for a symbol's exact source, or
   `get_file_outline(file_path=…)` for a file's structure. Prefer both over reading a whole
   file.
5. **Multi-hop and aggregates:** `query_graph` with a Cypher-like query, for questions
   `search_graph` and `trace_path` can't answer in one step. Examples: fan-out, dead code,
   every implementation of an interface. Use `get_graph_schema` first to see the labels and
   edge types.
6. **Literals:** `search_code` for strings, config keys, error messages and SQL, where a
   symbol search doesn't apply.
7. **Impact:** `detect_changes` to see what a change set affects, and `trace_path` inbound
   from each changed symbol.

## Efficiency rules

- Graph first, files second. A symbol question answered by `search_graph` plus
  `get_code_snippet` costs a fraction of opening files.
- Ask narrow questions. Use filters and small limits, and paginate with the `offset` or
  `cursor` the result returns instead of raising the limit. Most tools take
  `max_output_tokens` to cap a response.
- With several repositories attached, pass `repo` to query one. Without it, a tool may
  mix results across repositories or pick one for you.
- **Check coverage before relying on an answer.** Call `check_index_coverage` for every
  file your conclusion rests on. Where it reports lines the graph missed, read those lines
  directly and qualify the conclusion.
- Don't repeat orientation calls. Remember what `get_architecture` told you.
- Name what you relied on: cite `file:line` from the snippets, so the answer can be
  checked.

## When to read files instead

- Files you or the user changed locally since the last index.
- Non-code files: configuration, docs, fixtures, migrations in a DSL the graph doesn't
  parse.
- Anything `check_index_coverage` reports as not covered.
