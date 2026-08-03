# Seam changes the ui lane must absorb on its next rebase (lesson 23)

**From:** harness lane · **Date:** 2026-08-03 · **Context:** lesson 23
(`b20e271`) changes how a subagent's registry is derived: **filter +
substitute** instead of filter alone (rationale in
`docs/superpowers/specs/2026-08-03-subagent-registry-derivation-design.md`).
Both changes are backward-compatible — existing ui calls keep working
unchanged — but one removes an exclusion guarantee the ui could have keyed on.

## 1. `skill_tool(...)`: `spawns_subagents` is now conditional

The signature is unchanged — `skill_tool(skills, run=None, fork_run=None)` —
but the guard was hardcoded `spawns_subagents=True` and is now:

```python
spawns_subagents=fork_run is not None
```

The flag tracks the *capability*, not the tool's identity: only a fork-capable
build can delegate. Built without `fork_run`, a fork skill is refused
(`"Error: this skill runs as a subagent, which is unavailable here."`), so
that build cannot recurse — which is exactly why it is considered safe.

- **Consequence:** `skill_tool(skills, run)` with no `fork_run` is **no longer
  filtered out of subagent registries**. If the ui built one and relied on
  subagents never seeing it, that assumption is now false — though such a
  build also cannot delegate, so nothing unsafe leaks.
- Today this affects no ui code: `origin/ui/scaffold` registers **no** skill
  tool at all (`git grep skill origin/ui/scaffold -- ui/` → no matches).
  `view_skill_tool` (the read-only viewer from the 2026-07-11 note) is
  untouched — still `spawns_subagents=False`, still available in subagents.

## 2. `run_subagent()` / `agent_tool()` accept `substitutions`

Both gained an optional keyword, `substitutions: dict[str, Tool] | None =
None`. A subagent's registry is now derived filter-then-substitute
(`harness/tools/agent.py`):

```python
inner = {name: t for name, t in tools.items() if not t.spawns_subagents}
for name, variant in (substitutions or {}).items():
    if name in tools:  # never smuggle a tool past the caller's restriction
        inner[name] = variant
```

- **Purely additive.** Omitting it preserves the old prune-only behavior
  exactly — the ui's `agent_tool(...)` call in `ui/server/runner.py` needs no
  change on rebase.
- **What it buys you:** hand subagents a *different build* of a tool the
  parent holds. `main.py` now does `sub_variants["skill"] =
  skill_tool(skills, run)` (no `fork_run`) and passes
  `substitutions=sub_variants` to both `agent_tool` and the fork path — that
  is how subagents get fully expanding skills with fork skills refused. If the
  ui ever registers the executing `skill` tool, mirror this rather than
  stripping the skills menu from the subagent prompt (`main.py`'s
  `subagent_sections` workaround is gone for the same reason).
- **Guard:** a substitution applies only to a name present in the caller's
  `tools` dict, so a restricted registry (e.g. a fork skill's
  `allowed-tools`) stays authoritative — nothing can be injected past it.
