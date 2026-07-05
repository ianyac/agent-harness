from harness.prompts import Environment, build_system_prompt


def sample_env() -> Environment:
    # no fact may be a substring of another, or the containment
    # assertions below go vacuous
    return Environment(
        cwd="/home/yc/checkout/sub",
        workspace="/srv/agent-ws",
        os="Darwin 25.5.0",
        date="2026-07-05",
    )


def test_prompt_states_an_agent_identity():
    assert "agent" in build_system_prompt(sample_env()).lower()


def test_prompt_includes_every_environment_fact():
    prompt = build_system_prompt(sample_env())
    for fact in ("/home/yc/checkout/sub", "/srv/agent-ws", "Darwin 25.5.0", "2026-07-05"):
        assert fact in prompt


def test_prompt_gives_tool_use_guidance():
    assert "tool" in build_system_prompt(sample_env()).lower()


def test_extra_sections_are_appended_after_the_core_sections():
    prompt = build_system_prompt(sample_env(), extra_sections=["ALPHA", "BETA"])
    # anchor on the LAST core section, so extras spliced mid-prompt fail
    assert prompt.index("ground truth") < prompt.index("ALPHA") < prompt.index("BETA")


def test_no_extra_sections_is_the_default():
    # a bare env produces a valid prompt with no trailing junk and
    # nothing after the final core section
    prompt = build_system_prompt(sample_env())
    assert prompt.endswith("assumptions.")
    assert prompt.strip() == prompt
