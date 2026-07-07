from harness.skills import Skill, discover, skills_section, view_skill_tool


def write_skill(skills_dir, name, description, body):
    skills_dir.mkdir(exist_ok=True)
    (skills_dir / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}"
    )


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


def test_view_skill_returns_the_full_body(tmp_path):
    write_skill(tmp_path, "commit-style", "how to write commits", "Use imperative mood.")
    tool = view_skill_tool(discover(tmp_path))
    assert "Use imperative mood." in tool.execute(name="commit-style")


def test_view_skill_is_read_only(tmp_path):
    write_skill(tmp_path, "x", "d", "b")
    assert view_skill_tool(discover(tmp_path)).read_only is True


def test_view_skill_on_an_unknown_name_lists_what_exists(tmp_path):
    write_skill(tmp_path, "commit-style", "d", "b")
    write_skill(tmp_path, "review-style", "d", "b")
    result = view_skill_tool(discover(tmp_path)).execute(name="nope")
    assert result.startswith("Error")
    assert "commit-style" in result and "review-style" in result
