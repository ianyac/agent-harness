from harness.skills import (
    Skill,
    cmd_blocks,
    discover,
    expand_body,
    has_cmd_blocks,
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


def test_view_skill_tool_alias_returns_a_read_only_tool(tmp_path):
    write_skill(tmp_path, "x", "d", "body")
    tool = view_skill_tool(discover(tmp_path))
    assert tool.read_only is True
    assert tool.execute(name="x") == "body"


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


def test_substitute_args_arguments_and_positionals():
    assert substitute_args("all=$ARGUMENTS first=$1 third=$3", "a b") == "all=a b first=a third="


def test_substitute_args_fills_a_positional_inside_a_command():
    assert substitute_args("!`git log $1`", "HEAD") == "!`git log HEAD`"


def test_discover_reads_fork_model_and_allowed_tools(tmp_path):
    write_dir_skill(
        tmp_path, "research", "research", "does research",
        "body",
        # frontmatter written directly to control the extra keys:
    )
    # overwrite SKILL.md with the policy frontmatter
    (tmp_path / "research" / "SKILL.md").write_text(
        "---\nname: research\ndescription: d\ncontext: fork\n"
        "model: gpt-5.4-mini\nallowed-tools: read_file list_dir\n---\nBody."
    )
    (skill,) = discover(tmp_path)
    assert skill.fork is True
    assert skill.model == "gpt-5.4-mini"
    assert skill.allowed_tools == ["read_file", "list_dir"]


def test_discover_skips_a_skill_with_an_unknown_model(tmp_path):
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "SKILL.md").write_text(
        "---\nname: bad\ndescription: d\nmodel: gpt-9-imaginary\n---\nBody."
    )
    write_skill(tmp_path, "good", "d", "b")
    warnings = []
    skills = discover(tmp_path, on_warning=warnings.append)
    assert [s.name for s in skills] == ["good"]
    assert warnings and "gpt-9-imaginary" in warnings[0]


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
