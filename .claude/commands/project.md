---
description: Start or switch to a research project — scaffolds its drafts/notes/clones subfolders and scopes the session to it
argument-hint: <project_name>
allowed-tools: Bash(mkdir:*), Bash(find:*), Bash(echo:*), Bash(tr:*)
---

## Active project setup

Scaffolding subfolders (created only if missing) and listing any existing files for this project:

!`raw="$ARGUMENTS"; p=$(echo "$raw" | tr ' ' '_' | tr -cd '[:alnum:]_-'); [ -z "${p//_/}" ] && p=default; base=/workspace/target-state/research; for d in drafts notes clones; do mkdir -p "$base/$d/$p"; done; echo "Active project slug: $p"; [ "$p" = default ] && echo "(no name given → using the default project)"; echo "--- existing files ---"; found=$(find "$base/drafts/$p" "$base/notes/$p" "$base/clones/$p" -type f 2>/dev/null | sort); if [ -z "$found" ]; then echo "(none — this is a new/empty project)"; else echo "$found"; fi`

## Scoping rules for this session

For the **rest of this session**, treat the project slug shown above as the single active research project. Apply these rules until I say otherwise:

- **Read context only** from that project's three subfolders:
  - `/workspace/target-state/research/drafts/<slug>/`
  - `/workspace/target-state/research/notes/<slug>/`
  - `/workspace/target-state/research/clones/<slug>/`
- **Write every new file** into the matching subfolder, following the harness taxonomy:
  - apply-bound deliverables + the candidate plan → `drafts/<slug>/`
  - investigation write-ups, analyses, inventories, scratch reasoning → `notes/<slug>/`
  - disposable git checkouts → `clones/<slug>/`
- The candidate plan for this project goes at `drafts/<slug>/<plan_id>.yaml` (still schema-validated against `/workspace/plans/schema.json`).
- Do **not** pull in files from other projects' subfolders unless I explicitly ask.

Before doing anything else, read any existing files listed above so you have this project's prior context, then briefly confirm the active project and summarize what you found (or note that it's empty).
