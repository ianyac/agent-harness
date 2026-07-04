import pytest

from harness.tools.list_dir import list_dir_tool
from harness.tools.read_file import read_file_tool
from harness.tools.write_file import write_file_tool


def fs_registry():
    tools = [read_file_tool(), write_file_tool(), list_dir_tool()]
    return {tool.name: tool for tool in tools}


def test_write_then_read_round_trip(tmp_path):
    tools = fs_registry()
    target = tmp_path / "notes.txt"
    confirmation = tools["write_file"].execute(path=str(target), content="hello")
    assert "5" in confirmation and "notes.txt" in confirmation
    assert tools["read_file"].execute(path=str(target)) == "hello"


def test_write_overwrites_existing_content(tmp_path):
    tools = fs_registry()
    target = tmp_path / "x.txt"
    tools["write_file"].execute(path=str(target), content="old")
    tools["write_file"].execute(path=str(target), content="new")
    assert tools["read_file"].execute(path=str(target)) == "new"


def test_list_dir_marks_directories(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.txt").write_text("x")
    out = fs_registry()["list_dir"].execute(path=str(tmp_path))
    assert out.splitlines() == ["a.txt", "sub/"]


def test_read_missing_file_raises_for_now(tmp_path):
    # lesson 8 converts crashes into error results the model can read
    with pytest.raises(FileNotFoundError):
        fs_registry()["read_file"].execute(path=str(tmp_path / "nope.txt"))


def test_every_fs_tool_definition_is_well_formed():
    for name, tool in fs_registry().items():
        d = tool.definition()
        assert d["function"]["name"] == name
        assert len(d["function"]["description"]) > 20
