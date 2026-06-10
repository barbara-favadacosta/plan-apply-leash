---
description: Start or switch to a research project — scaffolds its drafts/notes/clones subfolders and scopes the session to it
argument-hint: <project-slug>
allowed-tools: Bash(test:*), Bash(mkdir:*), Bash(find:*), Bash(sort:*)
---

## Active project setup

Active project: **$ARGUMENTS** _(no name given → the `default` project)_

> Pass a simple slug — lowercase, dashes, no spaces or punctuation (e.g. `/project june-s3-buckets`). The name is used verbatim as the folder name, so a clean slug keeps the layout tidy. With no argument, the `default` project is used.

Scaffolding the three subfolders (created only if missing):

!`test -z "$ARGUMENTS" && mkdir -p "/workspace/target-state/research/drafts/default" "/workspace/target-state/research/notes/default" "/workspace/target-state/research/clones/default" || mkdir -p "/workspace/target-state/research/drafts/$ARGUMENTS" "/workspace/target-state/research/notes/$ARGUMENTS" "/workspace/target-state/research/clones/$ARGUMENTS"`

Existing files for this project (an empty list means it's new/empty):

!`test -z "$ARGUMENTS" && find "/workspace/target-state/research/drafts/default" "/workspace/target-state/research/notes/default" "/workspace/target-state/research/clones/default" -type f 2>/dev/null | sort || find "/workspace/target-state/research/drafts/$ARGUMENTS" "/workspace/target-state/research/notes/$ARGUMENTS" "/workspace/target-state/research/clones/$ARGUMENTS" -type f 2>/dev/null | sort`

## Scoping rules for this session

For the **rest of this session**, treat `$ARGUMENTS` (or `default` if no name was given) as the single active research project. Apply these rules until I say otherwise:

- **Read context only** from that project's three subfolders under `/workspace/target-state/research/`:
  - `drafts/<slug>/`
  - `notes/<slug>/`
  - `clones/<slug>/`
- **Write every new file** into the matching subfolder, following the harness taxonomy:
  - apply-bound deliverables + the candidate plan → `drafts/<slug>/`
  - investigation write-ups, analyses, inventories, scratch reasoning → `notes/<slug>/`
  - disposable git checkouts → `clones/<slug>/`
- The candidate plan for this project goes at `drafts/<slug>/<plan_id>.yaml` (still schema-validated against `/workspace/plans/schema.json`).
- Do **not** pull in files from other projects' subfolders unless I explicitly ask.

Before doing anything else, read any existing files listed above so you have this project's prior context, then briefly confirm the active project and summarize what you found (or note that it's empty).
