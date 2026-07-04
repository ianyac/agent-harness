from harness.tools.bash import bash_tool
from harness.truncate import truncate


def test_echo_round_trip():
    out = bash_tool().execute(command="echo hello")
    assert "hello" in out
    assert "exit code: 0" in out


def test_nonzero_exit_is_reported_in_band():
    out = bash_tool().execute(command="exit 3")
    assert "exit code: 3" in out  # a failure is a result, not an exception


def test_stderr_is_captured_alongside_stdout():
    out = bash_tool().execute(command="echo oops >&2")
    assert "oops" in out


def test_timeout_becomes_a_result_not_an_exception():
    out = bash_tool(timeout=1).execute(command="sleep 5")
    assert "timed out" in out.lower()


def test_truncate_keeps_head_and_tail_with_a_marker():
    text = "\n".join(str(i) for i in range(10000))
    out = truncate(text, limit=200)
    assert len(out) < len(text)
    assert out.startswith("0\n1\n")  # head preserved
    assert out.rstrip().endswith("9999")  # tail preserved
    assert "truncated" in out


def test_truncate_leaves_short_text_untouched():
    assert truncate("short", limit=200) == "short"


def test_bash_output_is_truncated_when_huge():
    out = bash_tool(output_limit=500).execute(command="seq 1 100000")
    assert "truncated" in out
    assert len(out) < 5000
