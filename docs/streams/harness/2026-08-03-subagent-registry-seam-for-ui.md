# Seam changes the ui lane must absorb on its next rebase (lesson 23)

**From:** harness lane · **Date:** 2026-08-03 · **Context:** lesson 23
(`b20e271`) changes how a subagent's registry is derived: **filter +
substitute** instead of filter alone (rationale in
`docs/superpowers/specs/2026-08-03-subagent-registry-derivation-design.md`).
Both changes are backward-compatible — existing ui calls keep working
unchanged — but one removes an exclusion guarantee the ui could have keyed on.

## 0. `skill_tool(skills)` (no `run`) now marks skipped commands

Behaviour change to a seam the ui lane may already call: a build without `run`
used to return a body with each `` !`cmd` `` **as written**; it now renders
`skills.NOT_RUN` — `[skill command not run: this agent cannot run shell
commands]` — in its place. An escaped `` \!`cmd` `` is still shown verbatim.

Why: bare template text is indistinguishable, to the model reading the body,
from a command that ran and printed nothing, so a no-shell agent would answer
from a template as if it were live data.

- **Affects you if** the ui lane builds `skill_tool(skills)` with no `run` and
  asserts on the body text. `view_skill_tool` is **unchanged** — it still
  returns the body verbatim (it never expands), so a ui lane using only that
  tool needs no change.
- `skill_tool`'s model-facing `description` is now composed per build: it
  mentions shell only when `run` is wired, and subagent/fork only when
  `fork_run` is. Anything asserting on that exact string will need updating.

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

Both gained an optional keyword, typed `Substitutions` (exported from
`harness/tools/agent.py`):

```python
Substitutions = dict[str, Tool] | Callable[[dict[str, Tool]], dict[str, Tool]] | None
```

A subagent's registry is now derived filter-then-substitute. The **callable**
form receives the filtered registry and returns the mapping, so a caller can
pick a build from what the sub will *actually* hold:

```python
inner = {name: t for name, t in tools.items() if not t.spawns_subagents}
chosen = substitutions(inner) if callable(substitutions) else substitutions
for name, variant in (chosen or {}).items():
    if name not in tools:
        continue                      # never smuggle a tool past a restriction
    if variant.spawns_subagents: raise ValueError(...)   # no delegating variant
    if variant.name != name:     raise ValueError(...)   # key must match name
    inner[name] = variant
```

Note the two `ValueError`s: a substitution may not be a delegating tool, and
its dict key must equal `tool.name` (the loop dispatches on the key but
advertises the name — a divergence offers a tool the sub can never call).

- **Purely additive.** Omitting it preserves the old prune-only behavior
  exactly — the ui's `agent_tool(...)` call in `ui/server/runner.py` needs no
  change on rebase.
- **What it buys you:** hand subagents a *different build* of a tool the
  parent holds. `main.py` keeps two hook-wrapped builds and chooses per
  delegation — `skill_tool(skills, run)` for a sub that already holds `bash`,
  `skill_tool(skills)` (commands load but do not run) otherwise. That keeps
  `allowed-tools` a real capability bound: excluding `bash` genuinely excludes
  shell, instead of a skill's `` !`cmd` `` becoming a side-channel around it.
  If the ui ever registers the executing `skill` tool, mirror this rather than
  stripping the skills menu from the subagent prompt (`main.py`'s
  `subagent_sections` workaround is gone for the same reason). Note the menu
  itself is now derived per-sub: no menu when the sub lacks the tool, and only
  non-fork skills are listed, since a non-forking build refuses them.
- **Guard:** a substitution applies only to a name present in the caller's
  `tools` dict, so a restricted registry (e.g. a fork skill's
  `allowed-tools`) stays authoritative — nothing can be injected past it.
