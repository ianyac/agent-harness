from harness.prompts import Environment, build_system_prompt


def sample_env() -> Environment:
    return Environment(
        cwd="/home/yc/proj/sub",
        workspace="/home/yc/proj",
        os="Darwin 25.5.0",
        date="2026-07-05",
    )


def test_prompt_states_an_agent_identity():
    assert "agent" in build_system_prompt(sample_env()).lower()


def test_prompt_includes_every_environment_fact():
    prompt = build_system_prompt(sample_env())
    for fact in ("/home/yc/proj/sub", "/home/yc/proj", "Darwin 25.5.0", "2026-07-05"):
        assert fact in prompt


def test_prompt_gives_tool_use_guidance():
    assert "tool" in build_system_prompt(sample_env()).lower()


def test_extra_sections_are_appended_after_the_core_sections():
    prompt = build_system_prompt(sample_env(), extra_sections=["ALPHA", "BETA"])
    assert prompt.index("Darwin 25.5.0") < prompt.index("ALPHA") < prompt.index("BETA")


def test_no_extra_sections_is_the_default():
    # a bare env produces a valid prompt with no trailing junk
    prompt = build_system_prompt(sample_env())
    assert prompt.strip() == prompt
