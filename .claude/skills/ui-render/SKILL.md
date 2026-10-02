---
name: ui-render
description: Renders this project's UI design spec and design tokens, read live from the software factory, into an editable design file in Pencil (or Figma, when its MCP server is connected instead). Use when asked to render, draw, visualise, or open the UI design / wireframes / screens in Pencil or Figma. Invoke explicitly with /ui-render [screen or epic].
---

# ui-render

Turns the UI Designer's tool-agnostic output into real screens in a design
tool. Two documents are the only source of truth:

- `UI-TOKENS-<slug>.json`: W3C Design Tokens (DTCG): colours, typography,
  spacing, radii, shadows, breakpoints.
- `UI-DESIGN-<slug>.md`: the design spec: every screen's layout regions,
  components, sample content, states and navigation, written against the
  token paths.

Never invent screens, components or colours that are not in them.

## Step 0: Find the documents

Pass `project_id` from `.software-factory/project.json` to every `software-factory` tool
call. If the MCP server isn't connected, stop and point the user to `INTEGRATION.md`.

1. Call `list_artifacts`. Then call `read_artifact` with `agent="ui-designer"` for the
   newest `UI-TOKENS-*.json` and `UI-DESIGN-*.md`.
2. If neither exists, stop: the UI Designer hasn't run for this project yet. Suggest running
   it in the software factory.

If the user named a screen or an epic, render only that part; otherwise
render every screen in the spec's Screen Catalogue.

## Step 1: Pick the design tool

- **Pencil** (tools named `pencil`, e.g. `get_app_state`): go to Step 2.
- **Figma** (a Figma MCP server with write tools): go to Step 3.
- **Neither is connected:** stop and tell the user how to connect one. For
  Pencil, install the Pencil app or IDE extension and open it; its MCP server
  is registered while the app runs. Do not fall back to writing HTML.

## Step 2: Render in Pencil

1. Call `get_app_state` to see the open document. Ask the user which `.pen`
   file to use, or create a new one named after the project, if none is open.
2. Call `read_skill()` and every file it points to for the kind of design you
   are making (e.g. a web app). Follow it for all canvas work. Pencil's
   `execute` API is defined there, not here.
3. **Tokens first.** Create the tokens as Pencil variables, keeping the token
   paths as names (`color.brand.primary`). Every colour, font, size, radius
   and shadow on the canvas must be bound to a variable, never a raw value.
4. **Components.** Build each entry of the spec's Component Library once, as
   a reusable component with its variants and states.
5. **Screens.** One frame per screen, named exactly as its spec heading
   (`<EPIC-ID>-<ScreenName>`). Lay out its regions as the spec describes,
   place the components, and fill them with the spec's sample content, never
   placeholder text. Add the empty/loading/error states the spec lists as
   separate frames beside the main one.
6. Arrange the frames left to right in the order of the spec's Navigation
   Flow.
7. When an `execute` call fails, fix it with `edits` against the failed
   snippet, as the tool describes. Do not resend the whole snippet.

## Step 3: Render in Figma

Follow the same order as Step 2: variables from the tokens, then components,
then one frame per screen with the spec's sample content. Use the Figma MCP
server's own write tools and follow its instructions. If it can only read
designs and not write them, stop and say so.

## Step 4: Report

List the screens and components you rendered, anything in the spec you could
not express in the tool, and any token you had to approximate. Suggest
re-running the UI Designer if the spec itself needs to change: edits made on
the canvas do not flow back into the pipeline.

## Rules

- The design spec and tokens are authoritative; the canvas is a rendering of
  them, not a place to redesign.
- Bind to token variables; never hard-code a value the tokens define.
- Do not modify the spec documents or the project's code.
