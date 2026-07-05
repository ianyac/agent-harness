import pytest

from harness.tools.list_dir import list_dir_tool
from harness.tools.read_file import read_file_tool
from harness.tools.write_file import write_file_tool


def confined_registry(root):
    tools = [
        read_file_tool(workspace=root),
        write_file_tool(workspace=root),
        list_dir_tool(workspace=root),
    ]
    return {tool.name: tool for tool in tools}


def test_write_and_read_inside_the_workspace(tmp_path):
    tools = confined_registry(tmp_path)
    tools["write_file"].execute(path="notes.txt", content="hi")
    assert tools["read_file"].execute(path="notes.txt") == "hi"
    assert (tmp_path / "notes.txt").read_text() == "hi"


def test_absolute_path_escape_is_refused(tmp_path):
    with pytest.raises(PermissionError, match="outside the workspace"):
        confined_registry(tmp_path)["read_file"].execute(path="/etc/hosts")


def test_dotdot_escape_is_refused(tmp_path):
    (tmp_path.parent / "secret.txt").write_text("nope")
    with pytest.raises(PermissionError, match="outside the workspace"):
        confined_registry(tmp_path)["read_file"].execute(path="../secret.txt")


def test_symlink_escape_is_refused(tmp_path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret")
    (tmp_path / "link").symlink_to(outside)
    # resolve() follows the symlink to its real location, which is outside
    with pytest.raises(PermissionError, match="outside the workspace"):
        confined_registry(tmp_path)["read_file"].execute(path="link")


def test_write_escape_is_refused_and_creates_nothing(tmp_path):
    target = tmp_path.parent / "escapee.txt"
    with pytest.raises(PermissionError):
        confined_registry(tmp_path)["write_file"].execute(
            path="../escapee.txt", content="pwned"
        )
    assert not target.exists()


def test_unconfined_tools_still_work_without_a_workspace(tmp_path):
    # no workspace passed → no confinement (pre-lesson-9 behavior preserved)
    target = tmp_path / "x.txt"
    write_file_tool().execute(path=str(target), content="ok")
    assert read_file_tool().execute(path=str(target)) == "ok"
