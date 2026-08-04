# Subagent Registry Derivation (Lesson 23)

**Date:** 2026-08-03
**Status:** Approved

**Context:** A subagent's tool registry is derived from the parent's by
*pruning* — `{n: t for n, t in tools.items() if not t.spawns_subagents}`.
Pruning can only remove, so a tool that is unsafe in **one configuration only**
must be deleted outright. Lesson 20 hit this: the skill tool was marked
`spawns_subagents=True` to bar nested fork, which cost subagents skills
entirely and forced `main.py` to strip the skills menu from their prompt (a
lesson 18–22 review finding) so they would not be told to call a tool they do
not have.

## Goal

Subagents get **fully expanding** skills — `` !`cmd` `` and `$ARGUMENTS` both —
with `context: fork` skills cleanly refused and nested fork still impossible.
Achieved by making registry derivation **filter + substitute** rather than
filter alone.

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| The guard's meaning | `spawns_subagents=fork_run is not None` | The skill tool is not inherently a delegator — only a fork skill delegates. Built without `fork_run` it already refuses fork skills, so it *cannot* recurse and needs no guard. The flag should track the capability, not the tool's identity |
| Derivation | filter, then apply `substitutions` | The minimum generalization that lets one tool appear in a subagent in a *different configuration*. Plain addition/removal is already expressible |
| Substitution scope | only names present in the caller's `tools` | A fork skill's `allowed-tools` passes an already-restricted dict; without this, substitution would smuggle `skill` back into a subagent that deliberately excluded it. The caller's offer stays authoritative |
| Hook coverage | variants pass through `with_hooks` too | A subagent's calls are model-driven, so they must be hook-gated — unlike the human's slash command, which deliberately uses the raw tool |
| `` !`cmd` `` in subagents | allowed (`run` is wired) | Same session-approved command set, same sandboxed `run` closure, same config-lane reasoning as the parent. Denying it would make skills half-functional in subs for no added safety |
| The skills menu | restored for subagents | The workaround existed only because pruning was the sole option; subs now hold a `skill` tool, so the menu is honest again and `subagent_sections` collapses back into `context_sections` |

## Components

- **`harness/skills.py`**: `skill_tool(...)` sets
  `spawns_subagents=fork_run is not None`. No other change — a fork skill
  invoked through a non-forking build already returns
  `"Error: this skill runs as a subagent, which is unavailable here."`
- **`harness/tools/agent.py`**: `run_subagent(..., substitutions=None)`:

  ```python
  inner = {n: t for n, t in tools.items() if not t.spawns_subagents}
  for name, variant in (substitutions or {}).items():
      if name in tools:          # only substitute what the caller offered
          inner[name] = variant
  ```

  `agent_tool(..., substitutions=None)` forwards it.
- **`main.py`**: a `sub_variants` dict, created empty before `agent_tool` (which
  closes over it) and filled inside the `if skills:` block with
  `skill_tool(skills, run)` — no `fork_run`. Wrapped by `with_hooks` alongside
  `tools`. Passed to both `agent_tool` and `fork_run`'s `run_subagent` call.
  `subagent_sections` is deleted; `subagent_prompt` uses `context_sections`.

## Data flow

```
parent registry:   {read_file, write_file, list_dir, bash, agent, skill*, exit_plan_mode}
                                                   (*fork-capable, spawns_subagents=True)
sub_variants:      {skill: <no fork_run, spawns_subagents=False>}

run_subagent(tools=parent, substitutions=sub_variants)
  filter     -> drops agent, skill*, exit_plan_mode
  substitute -> skill (non-forking) restored, because "skill" was in the offer
  result     -> {read_file, write_file, list_dir, bash, skill}

fork skill with allowed-tools: read_file
  caller's offer = {read_file}  -> "skill" absent -> no substitution
  result     -> {read_file}
```

## Error handling

- A fork skill invoked inside a subagent -> the existing `fork_run is None`
  error string; the sub sees an ordinary tool result and continues.
- A substitution naming a tool the caller never offered -> ignored (no
  injection past a restriction).
- `` !`cmd` `` failures inside a sub -> unchanged (`expand_body` degrades to
  `[skill command failed: ...]`).

## Testing

`tests/test_skills.py`:
- `skill_tool(skills, fork_run=...)` -> `spawns_subagents is True`;
  `skill_tool(skills)` -> `False`. Replaces
  `test_skill_tool_is_read_only_and_bars_nested_fork`, which asserted the guard
  on a tool built *without* `fork_run` — the exact conflation being removed.

`tests/test_agent_tool.py`:
- a substitution replaces a filtered tool in the sub's registry;
- a substitution for a name the caller did not offer is ignored;
- substitution does not mutate the parent's registry.

`tests/helpers.py`: `noop_tool` gains optional `name` / `spawns_subagents`
(defaults unchanged) so the new tests need no second factory.

## Revisions after code review

The first implementation was reviewed and revised. The review found that
handing subagents an *executing* skill tool quietly broke two things:

1. **`allowed-tools` stopped being a capability bound.** A fork skill listing
   `skill` but not `bash` still got shell, because any skill's `` !`cmd` ``
   runs through the same `run` closure. **Fix:** `substitutions` now also
   accepts a *callable* given the sub's filtered registry, and `main.py` keeps
   two hook-wrapped builds — `skill_tool(skills, run)` for a sub that already
   holds `bash`, `skill_tool(skills)` (no `run`) otherwise. Excluding `bash`
   once again excludes shell.
2. **A refused fork skill ran its commands first.** `execute` expanded the body
   (executing `` !`cmd` ``) *before* checking `skill.fork`, so a sub calling a
   fork skill fired its side effects, got a bare error, and typically retried —
   firing them twice. **Fix:** refuse before expanding, and say "Do not retry".

Three honesty fixes followed from the same seam: the sub's menu is now built
from the registry that sub actually receives (no menu when it lacks `skill`),
lists only non-fork skills (its build refuses forks), and the `allowed-tools`
validation runs *after* `skill` is registered so a legitimate
`allowed-tools: skill` is no longer warned about.

Two invariants are now enforced in `run_subagent` rather than left to
convention: a substitution must not be a delegating tool, and its key must
match `tool.name` (the loop dispatches on the key but advertises the name).
Both raise `ValueError`.

### Second review round

The bash-gating fix above was itself reviewed and found to have traded one
defect for a subtler one, plus fourteen smaller issues. The root problem: the
no-shell build used `run=None`, and `expand_body` re-emitted each `` !`cmd` ``
as **bare template text** — indistinguishable to a model from a command that
ran and printed nothing. A bash-less subagent would read `` !`git status` ``,
conclude the tree was clean, and return that as its answer.

Fixed at the root rather than at the call site: `run=None` now renders
`skills.NOT_RUN` (`[skill command not run: …]`) for each command, while an
escaped `` \!`cmd` `` still shows exactly as written. One mechanism serves both
"this build may not execute" and the existing no-sandbox notice.

The rest, in three groups:

- **Honesty.** The skill tool's description is now *composed per build*, so a
  build never advertises fork or shell it lacks; the not-found error lists only
  skills that build can serve; the subagent menu drops command-bearing skills
  when the sub cannot run them; the `agent` tool no longer promises "the same
  tools".
- **The gate.** Extracted to `main.may_run_skill_commands(offered, mode)` — a
  named, tested predicate rather than an inline conditional (the inline form
  survived mutation with all tests green). It also now returns False during a
  plan turn, so the sub's prompt no longer says "commands are denied" while
  handing it the one build that could run them. Shell is recognised via
  `SHELL_TOOLS`; a foreign (MCP) shell is not recognised and fails closed —
  visibly, since the resulting build marks every skipped command.
- **The seam.** A callable `substitutions` receives a **copy** of the filtered
  registry (mutating the original would have written past both guards), and the
  invariants are validated across the whole map *before* any of it is applied,
  so a bad substitution fails identically on every delegation instead of only
  when that name happens to be offered.

## Out of scope (deferred)

- The fork `compact_threshold` derived from the parent's context window.
- `allowed-tools` validation tidy-up.
- Any per-subagent *addition* of tools the parent lacks.
