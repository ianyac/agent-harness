# Skill Directories & Bundled Files (Lesson 19)

**Date:** 2026-07-09
**Status:** Approved pending user review

**Arc:** Lesson 19 of the full-skill-tool arc — 18 dynamic `` !`cmd` `` injection
(done), **19 directories + bundling** (this spec), 20 args + frontmatter-as-policy.
`args` (`$1`/`$ARGUMENTS`) is deferred out of 19: an argument isn't known until
invocation, so it can't appear in a `` !`cmd` `` the human approves at session
start (lesson 18's model). `${SKILL_DIR}` has no such problem — it's fixed at
discovery.

## Goal

A skill may be a directory — `skills/<name>/SKILL.md` — that bundles files
(`scripts/`, `references/`, …). The body references them via `${SKILL_DIR}`, and
the model loads them on demand with the existing `read_file` tool: the **third
progressive-disclosure tier** (menu → `SKILL.md` → bundled files). Legacy flat
`skills/<name>.md` skills keep working (Claude Code parity).

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Layout | Support **both** flat `<name>.md` and `<name>/SKILL.md` | Claude Code keeps legacy flat files working ("superseded by" dirs); lesson 18's skills + tests stay stable, dirs are purely additive |
| Skill identity | Frontmatter `name` (not the dir name) | Consistent with lesson 18; the dir is just where bundled files live (a dir `foo/` may hold a skill named `bar`) |
| `${SKILL_DIR}` resolution | Substituted at **discovery** (to the skill's absolute dir) before `Skill.body` is stored | Every downstream consumer — `cmd_blocks` (the approval listing), `expand_body`, the injected body — then sees a real path. Works precisely because the path is fixed at discovery (the property `args` lacks) |
| Dir without `SKILL.md` | Ignored silently | Not every subdirectory is a skill; a warning would be noise on unrelated folders |
| Flat skill's `${SKILL_DIR}` | The `skills/` root | Harmless; flat skills rarely reference it |
| Third tier | Emergent — convention + `read_file`, **no new mechanism** | Body says "read `${SKILL_DIR}/references/api.md`"; model reads it on demand. Bundled files sit under the workspace, so file-tool confinement allows it |
| `args` (`$1`/`$ARGUMENTS`) | Deferred to the next lesson | Interacts with lesson 18's session-approval; its own concept |
| `${SKILL_DIR}` escape / literal | None (YAGNI) | A skill needing a literal `${SKILL_DIR}` is an unmotivated edge case |

## Components

- **`harness/skills.py`**:
  - `Skill` gains `dir: Path` — what `${SKILL_DIR}` resolves to.
  - `discover` reads both layouts and substitutes `${SKILL_DIR}` into the body.
  - `_parse` (frontmatter → name/description/body) is **reused unchanged** for
    `SKILL.md`.
- **`main.py`**: unchanged — same `discover(workspace / "skills")` call, same
  session gate, same wiring.
- Unchanged: `skill_tool`, `expand_body`, `_CMD`/`cmd_blocks`/`has_cmd_blocks`,
  `skills_section` (the menu).

## Discovery logic

```
skills, seen = [], set()
for entry in sorted(skills_dir.iterdir()):            # deterministic order
    if entry is a file "<name>.md":
        text, dir = entry.read_text(), skills_dir      # flat (legacy)
    elif (entry / "SKILL.md").is_file():
        text, dir = (entry / "SKILL.md").read_text(), entry   # directory skill
    else:
        continue                                       # not a skill dir
    try: name, description, body = _parse(text)
    except (OSError, ValueError, UnicodeDecodeError): warn + continue
    body = body.replace("${SKILL_DIR}", str(dir))      # resolve the fixed path
    if name in seen: warn + continue                   # keep first
    seen.add(name); skills.append(Skill(name, description, body, dir))
return sorted(skills, key=name)                         # menu order
```

Iteration is sorted for determinism; final list re-sorted by `name` (a dir named
`z/` may hold skill `a`).

## Data flow — the third tier

```
system prompt: menu (name + description only)
  → model calls skill(name="pdf-tools")
    → SKILL.md body injected, with ${SKILL_DIR} already = /abs/skills/pdf-tools
      → body: "For the field list, read /abs/skills/pdf-tools/references/fields.md"
        → model calls read_file(/abs/skills/pdf-tools/references/fields.md)  ← tier 3, on demand
```

## Path form — absolute (confirmed)

`${SKILL_DIR}` resolves to the skill's **absolute** directory. Both consumers
handle that correctly (checked against the code, not assumed):
- `read_file` → `resolve_in_workspace` (`harness/tools/workspace.py`) computes
  `(root / path).resolve()`; an absolute right operand to `/` replaces `root`, so
  an absolute path *inside* the workspace resolves to itself and passes the
  `root in candidate.parents` check — accepted. (Paths *outside* still raise.)
- A `` !`cmd` `` runs via `run_sandboxed`, which sets no `cwd`, so a *relative*
  path would resolve against the process cwd, not the workspace. Absolute
  sidesteps that. Bundled files sit under `skills/` (inside the workspace), so
  the sandbox permits reading/executing them (writes stay workspace-confined).

## Error handling

- Dir without `SKILL.md` → skipped (not an error).
- Malformed `SKILL.md` → skipped with a warning (via `_parse`'s `ValueError`),
  same as a malformed flat file — one bad skill never sinks the others.
- Duplicate `name` across layouts → keep the first, warn (the lesson-15/18 rule).

## Testing (`tests/test_skills.py`)

- A directory skill is discovered: `dir` set to the skill's directory, body parsed.
- A flat skill is still discovered (backward compatibility).
- Flat and directory skills discovered side by side.
- A directory without `SKILL.md` is ignored.
- `${SKILL_DIR}` in a body resolves to the skill's directory.
- `${SKILL_DIR}` inside a `` !`cmd` `` → `cmd_blocks` reports the **resolved** path
  (approval honesty).
- A flat skill's `${SKILL_DIR}` resolves to the `skills/` root.
- Frontmatter `name` differing from the dir name → identity is the frontmatter name.
- Add a `write_dir_skill` test helper.

## Out of scope (deferred)

- `args` / `$1` / `$ARGUMENTS` — next lesson.
- Frontmatter-as-policy (`allowed-tools`, `model`, `context: fork`) — later.
- `name` defaulting to the dir name (frontmatter `name` stays required).
- A `${SKILL_DIR}` escape/literal.
