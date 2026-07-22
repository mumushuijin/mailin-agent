from app.tools.packages.filesystem.handlers import glob_search, list_directory


def test_list_directory_empty_path_defaults_to_root():
    result = list_directory("")
    assert "路径不能为空" not in result


def test_glob_search_empty_directory_defaults_to_root():
    result = glob_search("*.md", "")
    assert "路径不能为空" not in result
