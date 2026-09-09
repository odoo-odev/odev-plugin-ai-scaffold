# odev-plugin-ai-scaffold

Analyse an Odoo task with an AI agent, and scaffold the module it describes.

Two commands over the same material - a task, its diagrams, and the standard Odoo source
of the version the client runs - differing in what the agent is asked to produce:

| | |
| --- | --- |
| `odev analyze <task>` | Reads the task and writes an analysis: impacted models and fields, proposed implementation, an estimate per requirement. Written out in the conversation, to read and argue with. |
| `odev scaffold <task>` | Reads the same task and writes the module: manifest, models, views, security, then installs it to check that it works. |
| `odev quickstart <task>` | Sets up the database the task points at, then scaffolds into it. |

## Setup

The task is read over RPC from a single Odoo instance, named in `odev.cfg`:

```ini
[ai_scaffold]
task_url = https://www.odoo.com
task_database =
```

`task_database` is only needed for a host serving several databases; a monodb host names
itself. Credentials are the ones odev already keeps for that host, asked for once and
stored, the same way as for any other remote database.

Exporting the Excalidraw diagrams a task links to needs a browser. odev already
provisions one - the Chrome build Runbot pins, shared with `odev test` - and the export
reuses it, so there is usually nothing to install:

```console
$ pip install -r requirements.txt
```

If odev has no Chrome to lend (no `npx`, or provisioning failed), Playwright falls back
on its own bundled Chromium: `playwright install chromium`.

## What the agent is given

Everything the task carries, because a task is written for a person and the parts are
not separable without losing something:

- **the description**, as text
- **the images embedded in it**, exported as files the agent can open, and referred to
  from the description by that same file name so it knows which picture goes where
- **the Excalidraw diagrams it links to**, all of them, exported to SVG through a real
  browser - there is no url that hands back a picture, so the board is opened and its
  own export driven, in the same Chrome odev uses for tours. An Excalidraw+ read-only
  link opens a viewer with no export of its own: its scene is read off the page and
  handed to the editor, which loses the images of the board but keeps every label
- **the standard Odoo source** of the target version, mounted read-only, so the agent can
  tell what Odoo already does from what has to be built - only the second is estimated
- **the client's database**, optionally (`--context`), cloned and given as context

The Odoo version, the hosting and the client name are read off the analysed database
first, then the task, and asked for only when neither knows.

## Estimation

`analyze` grounds each estimate in a line count rather than a round guess, at
throughputs you can tune:

```ini
[ai_scaffold]
loc_per_hour_python = 20
loc_per_hour_xml = 50
loc_per_hour_js = 20
minimum_dev_hours = 4
saas_logic_hours = 10
```

On Odoo Online, `saas_logic_hours` caps what the analysis may propose as logic - server
actions, automation rules, sandboxed computes, the model and field records standing in
for a class - so the database stays simple enough to keep maintaining from the interface.
Views, QWeb, SCSS and JavaScript do not count against it: those would be written the same
way on Odoo.sh. Over the cap, or where the requirements only fit through workarounds, the
agent stops and asks whether the development should move to Odoo.sh before writing the
rest of the analysis.

## Extending it

The plugin knows one source of analyses (the task) and has nowhere to deliver one to. A
plugin that has both overrides three seams and nothing else:

| Seam | For |
| --- | --- |
| `analysis_factory` on the commands | Reading analyses from somewhere else, falling back to the task |
| `PromptFactory.extensions` | A mixin replacing `_get_reporting_prompt`, to deliver rather than print |
| `AnalyzeCommand._get_mcp_servers` | The tools the agent delivers through |
