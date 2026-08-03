# Seam changes the ui lane must absorb on its next rebase (lesson 18–22)

**From:** harness lane · **Date:** 2026-07-11 · **Context:** fixes to PR #16
(lessons 18–22) surfaced two seams the ui lane consumes. Both are fixed on the
harness side to stay backward-compatible, but the ui lane should adjust when it
rebases onto the merged stack.

## 1. `permissions.MODES` no longer contains `"plan"`

`MODES` is back to the **constructible** set: `("default", "acceptAll", "readOnly")`
(now `MODES == STARTUP_MODES`). Plan mode is a *runtime-only* `self.mode` set
per turn by `/plan`; `PermissionPolicy("plan")` **raises** (it would trap the
session with a non-escapable `base_mode`).

- If the ui serves `list(MODES)` as its startup mode picker and validates
  `body.mode in MODES`, this is already correct — it will no longer offer
  `"plan"`, and constructing a session in `"plan"` is (correctly) rejected.
- Do **not** re-add `"plan"` to the picker. To enter plan mode from the ui,
  drive it per-turn the way the REPL's `/plan` does (set `policy.mode = "plan"`
  for one turn), not as a startup/base mode.

## 2. `skills_section(skills, tool_name="skill")` — pass your tool's name

`skills_section` now takes the registered tool's name and builds the menu line
`"call the <tool_name> tool to load one in full"`. The default is `"skill"`
(the main loop's executing tool), so existing calls keep working. **The ui lane
registers `view_skill_tool` (name `"view_skill"`)**, so it should call:

```python
skills_section(skills, tool_name="view_skill")
```

Otherwise the menu tells the ui model to "call the skill tool", which doesn't
exist in the ui registry → `Error: unknown tool 'skill'`.

Related: `view_skill_tool` is again a **real** factory (not an alias to
`skill_tool`) — it returns a read-only, non-executing tool named `view_skill`
with `spawns_subagents=False`, i.e. the lesson-15 contract the ui lane keyed on.

## 3. `Skill.body` now carries an unresolved `${SKILL_DIR}` token

`discover()` no longer eagerly replaces `${SKILL_DIR}` in the body. Resolution
moved to invocation time (`expand_body` / `view_skill_tool.execute`), where it
is **shell-quoted inside a command** but **raw in prose** — a quoted path in
prose was corrupting `read_file` arguments on spaced install paths.

- Consume bodies through `view_skill_tool` (which resolves `${SKILL_DIR}` raw)
  or the `skill` tool — do **not** read `Skill.body` directly and expect a
  resolved path; you would now see the literal `${SKILL_DIR}` token. `Skill.dir`
  holds the directory if you need to resolve it yourself.
