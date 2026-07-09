# Lesson 19 — Skill Directories & Bundled Files Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A skill may be a `skills/<name>/SKILL.md` directory that bundles files, referenced from the body via `${SKILL_DIR}` (resolved at discovery to the skill's absolute directory) and loaded on demand by the model — the third disclosure tier. Legacy flat `skills/<name>.md` skills keep working.

**Architecture:** Two changes, both in `harness/skills.py`: `Skill` gains a `dir: Path` field, and `discover` learns to read both layouts and substitute `${SKILL_DIR}`. `main.py` is untouched — the session gate then simply lists resolved command paths.

**Tech Stack:** Python 3.14, `uv`, `pytest`, stdlib `pathlib`. No new dependencies.

## Global Constraints

- Python 3.14; `uv run pytest` offline; no new third-party dependencies.
- Commit style: `lesson 19: <what changed>`; commit body ends with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- Support BOTH flat `skills/<name>.md` and `skills/<name>/SKILL.md` — flat skills and all lesson-18 tests must keep passing.
- A skill's identity is its frontmatter `name`, not the directory name.
- `${SKILL_DIR}` resolves to the skill's **absolute** directory (verified: `resolve_in_workspace` accepts absolute-within-workspace paths). A flat skill's `${SKILL_DIR}` is the `skills/` root.
- A directory without `SKILL.md` is ignored silently. `main.py` must not change.
- Out of scope: `args`/`$1`/`$ARGUMENTS`, frontmatter-as-policy, a `${SKILL_DIR}` escape.

---

### Task 1: Directory discovery + `Skill.dir`

Teach `discover` to read `skills/<name>/SKILL.md` directories alongside flat `skills/<name>.md` files, and record each skill's directory. No `${SKILL_DIR}` yet (Task 2).

**Files:**
- Modify: `harness/skills.py` (the `Skill` dataclass; the `discover` function)
- Test: `tests/test_skills.py`

**Interfaces:**
- Produces: `Skill(name, description, body, dir: Path)` — `dir` is the directory `${SKILL_DIR}` resolves to (the skill's own dir for a directory skill; the `skills/` root for a flat skill). `discover(skills_dir, on_warning=print) -> list[Skill]` reads both layouts.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_skills.py` (a directory-skill helper + cases):

```python
def write_dir_skill(skills_dir, dirname, name, description, body, files=None):
    d = skills_dir / dirname
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}"
    )
    for relpath, content in (files or {}).items():
        f = d / relpath
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
    return d


def test_discover_reads_a_directory_skill(tmp_path):
    write_dir_skill(tmp_path, "pdf", "pdf", "work with pdfs", "Body.")
    (skill,) = discover(tmp_path)
    assert skill.name == "pdf"
    assert skill.body == "Body."
    assert skill.dir == tmp_path / "pdf"


def test_flat_skill_dir_is_the_skills_root(tmp_path):
    write_skill(tmp_path, "commit-style", "d", "Body.")
    (skill,) = discover(tmp_path)
    assert skill.dir == tmp_path


def test_discover_reads_flat_and_directory_skills_together(tmp_path):
    write_skill(tmp_path, "flat", "d", "flat body")
    write_dir_skill(tmp_path, "deep", "deep", "d", "dir body")
    assert [s.name for s in discover(tmp_path)] == ["deep", "flat"]


def test_a_directory_without_skill_md_is_ignored(tmp_path):
    (tmp_path / "notaskill").mkdir()
    (tmp_path / "notaskill" / "readme.txt").write_text("nothing here")
    write_dir_skill(tmp_path, "real", "real", "d", "b")
    assert [s.name for s in discover(tmp_path)] == ["real"]


def test_directory_name_and_frontmatter_name_may_differ(tmp_path):
    write_dir_skill(tmp_path, "tools", "pdf", "d", "b")  # dir 'tools', name 'pdf'
    (skill,) = discover(tmp_path)
    assert skill.name == "pdf"
    assert skill.dir == tmp_path / "tools"
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest tests/test_skills.py -k "directory or flat_skill_dir" -v`
Expected: FAIL — `discover` yields no directory skills / `Skill` has no `dir` (TypeError or AttributeError).

- [ ] **Step 3: Add `dir` to `Skill` and rewrite `discover`**

In `harness/skills.py`, extend the dataclass:

```python
@dataclass
class Skill:
    name: str
    description: str
    body: str
    dir: Path
```

Replace `discover` with:

```python
def discover(
    skills_dir: Path, on_warning: Callable[[str], None] = print
) -> list[Skill]:
    """Load every skill under skills/. A skill is either a flat `<name>.md`
    file or a `<name>/SKILL.md` directory (which may bundle files referenced
    from the body via ${SKILL_DIR}). A malformed skill is skipped with a
    warning, never fatal — one bad skill must not sink the others."""
    skills = []
    seen: set[str] = set()
    skills_dir = Path(skills_dir)
    entries = sorted(skills_dir.iterdir()) if skills_dir.is_dir() else []
    for entry in entries:
        if entry.is_file() and entry.suffix == ".md":
            source, base = entry, skills_dir  # flat skill (legacy)
        elif entry.is_dir() and (entry / "SKILL.md").is_file():
            source, base = entry / "SKILL.md", entry  # directory skill
        else:
            continue  # unrelated file, or a dir without SKILL.md — not a skill
        try:
            # utf-8-sig: read UTF-8 with or without a BOM (some editors add one)
            name, description, body = _parse(source.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, UnicodeDecodeError) as error:
            on_warning(f"skipping skill {entry.name}: {error}")
            continue
        if name in seen:
            # a duplicate name would shadow the first in the skill tool's lookup;
            # keep the first, never silently serve the wrong body
            on_warning(f"skipping skill {entry.name}: duplicate name {name!r}")
            continue
        seen.add(name)
        skills.append(Skill(name=name, description=description, body=body, dir=base))
    return sorted(skills, key=lambda s: s.name)  # menu order = displayed names
```

(`skills_dir.is_dir()` guard preserves the "missing dir → `[]`" behavior that `glob` gave for free.)

- [ ] **Step 4: Run the full suite to verify pass + no regression**

Run: `uv run pytest tests/test_skills.py -v`
Expected: PASS — the new tests and every pre-existing flat-skill test (the `dir` field is additive; flat tests read `.name`/`.body`).

- [ ] **Step 5: Commit**

```bash
git add harness/skills.py tests/test_skills.py
git commit -m "lesson 19: discover skills/<name>/SKILL.md directories

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `${SKILL_DIR}` substitution

Resolve `${SKILL_DIR}` in the body at discovery, to the skill's `dir`. Because it happens before `Skill.body` is stored, `cmd_blocks` (the session-approval listing) and `expand_body` both see real paths.

**Files:**
- Modify: `harness/skills.py` (`discover` — one line)
- Test: `tests/test_skills.py`

**Interfaces:**
- Consumes: `Skill.dir` (Task 1).
- Produces: `discover` returns skills whose `body` has every `${SKILL_DIR}` replaced by `str(dir)`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_skills.py`:

```python
def test_skill_dir_is_substituted_in_the_body(tmp_path):
    write_dir_skill(tmp_path, "pdf", "pdf", "d", "schema: ${SKILL_DIR}/references/api.md")
    (skill,) = discover(tmp_path)
    assert "${SKILL_DIR}" not in skill.body
    assert f"{tmp_path / 'pdf'}/references/api.md" in skill.body


def test_skill_dir_resolves_inside_a_command_for_the_approval_listing(tmp_path):
    write_dir_skill(tmp_path, "pdf", "pdf", "d", "run: !`python ${SKILL_DIR}/check.py`")
    (skill,) = discover(tmp_path)
    assert cmd_blocks(skill.body) == [f"python {tmp_path / 'pdf'}/check.py"]


def test_flat_skill_dir_substitutes_to_the_skills_root(tmp_path):
    write_skill(tmp_path, "s", "d", "here: ${SKILL_DIR}/x")
    (skill,) = discover(tmp_path)
    assert f"{tmp_path}/x" in skill.body
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest tests/test_skills.py -k "skill_dir_is_substituted or resolves_inside or substitutes_to" -v`
Expected: FAIL — `${SKILL_DIR}` remains literal in `skill.body`.

- [ ] **Step 3: Substitute at discovery**

In `harness/skills.py`, inside `discover`, add the substitution immediately after the `try/except` that parses the skill (before the duplicate-name check):

```python
        body = body.replace("${SKILL_DIR}", str(base))  # a fixed path, resolved once
```

- [ ] **Step 4: Run the full suite to verify pass**

Run: `uv run pytest tests/test_skills.py -v`
Expected: PASS (all skills tests).

- [ ] **Step 5: Full suite + import sanity**

Run: `uv run pytest && uv run python -c "import main"`
Expected: whole suite green; `main` imports cleanly (confirms nothing else depended on the old `Skill` shape).

- [ ] **Step 6: Commit**

```bash
git add harness/skills.py tests/test_skills.py
git commit -m "lesson 19: resolve \${SKILL_DIR} to the skill's directory at discovery

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

- **Spec coverage:** both layouts (Task 1) ✓; `Skill.dir` + frontmatter identity (Task 1 tests) ✓; dir-without-SKILL.md ignored (Task 1) ✓; `${SKILL_DIR}` resolved at discovery, absolute, honest in `cmd_blocks` (Task 2) ✓; flat skill's `${SKILL_DIR}` = skills root (Task 2) ✓; third tier is emergent (no code — the body references a real path the model `read_file`s, exercised implicitly) ✓; `main.py` unchanged ✓; args/frontmatter-policy deferred (Global Constraints) ✓.
- **Placeholder scan:** none — every step has full code and exact commands.
- **Type consistency:** `Skill(name, description, body, dir)` used identically in `discover` and asserted via `.dir` in tests; `discover`'s signature unchanged; `base` is the `Path` stored as `dir` and used in `str(base)` for the substitution.
- **Regression guard:** the `is_dir()` guard keeps `discover(missing) == []`; the `dir` field is additive so every lesson-18 flat test still reads `.name`/`.body` unchanged.
