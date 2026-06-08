#!/usr/bin/env python3
"""
stop-hook.py — Stop hook for the apply env (reference agent: Claude Code).

Reads the persistent tally written by pre-tool-hook.py and prints a human-readable
summary of the CURRENT SESSION at the end of each assistant turn. The tally
itself lives under target-state/<slug>/audit/tally.jsonl and persists across
container restarts and across sessions — entries carry session_id and we
filter to the current session for the per-turn summary.

Scope limit: only sees the tools pre-tool-hook.py guards (Bash/Edit/Write/Notebook).
Reads, Globs, Greps, WebFetches are not in the tally — that's intentional, this
is a write-side summary, not a full audit log of every action.

Exit 0 always.
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _paths


def fmt_target(target: str, max_len: int = 80) -> str:
    target = target.replace("\n", " ⏎ ")
    if len(target) <= max_len:
        return target
    return target[: max_len - 1] + "…"


def main() -> None:
    current_session = "?"
    try:
        payload = json.load(sys.stdin)
        current_session = payload.get("session_id", "?")
    except (json.JSONDecodeError, ValueError):
        pass

    tally_path = _paths.tally_path()
    if not tally_path.exists():
        sys.exit(0)

    entries: list[dict] = []
    try:
        with tally_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("session_id") == current_session:
                    entries.append(e)
    except OSError:
        sys.exit(0)

    if not entries:
        sys.exit(0)

    tool_decisions: dict[str, Counter] = defaultdict(Counter)
    blocks: list[dict] = []
    files_edited: set[str] = set()
    commands_run: list[str] = []

    for e in entries:
        tool = e.get("tool", "?")
        decision = e.get("decision", "?")
        target = e.get("target", "")
        tool_decisions[tool][decision] += 1
        if decision == "block":
            blocks.append(e)
        elif decision == "allow":
            if tool in ("Edit", "Write", "NotebookEdit") and target:
                files_edited.add(target)
            elif tool == "Bash" and target:
                commands_run.append(target)

    total = sum(sum(c.values()) for c in tool_decisions.values())
    total_blocked = sum(c["block"] for c in tool_decisions.values())

    bar = "─" * 60
    out = [bar, f"[apply session summary]  session_id={current_session}"]
    out.append(f"  total gated tool calls: {total}  (blocked: {total_blocked})")
    out.append("")

    for tool in sorted(tool_decisions):
        c = tool_decisions[tool]
        out.append(f"  {tool}: {c['allow']} allowed, {c['block']} blocked")

    if files_edited:
        out.append("")
        out.append(f"  files touched ({len(files_edited)}):")
        for path in sorted(files_edited):
            out.append(f"    • {fmt_target(path)}")

    if commands_run:
        out.append("")
        out.append(f"  commands run ({len(commands_run)}):")
        seen: set[str] = set()
        for cmd in commands_run:
            short = fmt_target(cmd, 60)
            if short in seen:
                continue
            seen.add(short)
            out.append(f"    • {short}")
            if len(seen) >= 10:
                remaining = len(commands_run) - 10
                if remaining > 0:
                    out.append(f"    … +{remaining} more")
                break

    if blocks:
        out.append("")
        out.append(f"  blocked ({len(blocks)}):")
        for b in blocks[:10]:
            tool = b.get("tool", "?")
            target = fmt_target(b.get("target", ""), 60)
            reason = b.get("reason", "")
            out.append(f"    • {tool}: {target}")
            out.append(f"        reason: {reason}")
        if len(blocks) > 10:
            out.append(f"    … +{len(blocks) - 10} more blocks")

    out.append("")
    out.append(f"  full audit log: {tally_path} (persists across sessions)")
    out.append(bar)
    print("\n" + "\n".join(out) + "\n", file=sys.stderr, flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
