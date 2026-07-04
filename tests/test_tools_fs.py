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


def test_read_offset_and_limit_return_a_slice_that_announces_itself(tmp_path):
    target = tmp_path / "big.txt"
    target.write_text("\n".join(f"line{i}" for i in range(1, 101)))
    out = fs_registry()["read_file"].execute(path=str(target), offset=10, limit=3)
    lines = out.splitlines()
    # a header announces the slice so the model knows to continue reading
    assert "11" in lines[0] and "13" in lines[0] and "100" in lines[0]
    assert lines[1:] == ["line11", "line12", "line13"]


def test_read_whole_small_file_has_no_slice_header(tmp_path):
    target = tmp_path / "small.txt"
    target.write_text("just one line")
    assert fs_registry()["read_file"].execute(path=str(target)) == "just one line"


def test_read_truncates_a_huge_file_without_offset(tmp_path):
    target = tmp_path / "huge.txt"
    target.write_text("x" * 50000)
    out = fs_registry()["read_file"].execute(path=str(target))
    assert "truncated" in out
    assert len(out) < 20000


def test_every_fs_tool_definition_is_well_formed():
    for name, tool in fs_registry().items():
        d = tool.definition()
        assert d["function"]["name"] == name
        assert len(d["function"]["description"]) > 20
