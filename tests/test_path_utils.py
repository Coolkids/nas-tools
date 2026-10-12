from pathlib import Path

from app.utils.path_utils import PathUtils


def test_extension_filter_excludes_dotfiles_without_extensions(tmp_path: Path):
    (tmp_path / ".secret_key").write_text("secret", encoding="utf-8")
    (tmp_path / "init.sql").write_text("SELECT 1", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("notes", encoding="utf-8")

    files = PathUtils.get_dir_level1_files(str(tmp_path), exts=".sql")

    assert [Path(path).name for path in files] == ["init.sql"]
