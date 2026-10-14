from types import SimpleNamespace

import app.filetransfer as filetransfer_module
from app.utils.types import MediaType, RmtMode, SyncType


class _FakeMedia:
    type = MediaType.MOVIE
    tmdb_info = {"id": 990001}
    category = ""

    def get_title_string(self):
        return "fixture movie"


def _transfer(tmp_path, *, destination_exists=False, udf_flag=True):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    media_file = source / "fixture.mkv"
    media_file.write_bytes(b"private fixture")
    destination = target / "recognized"
    if destination_exists:
        destination.mkdir()

    transfer = filetransfer_module.FileTransfer.__new__(filetransfer_module.FileTransfer)
    transfer.media = SimpleNamespace(
        get_media_info_on_files=lambda *args, **kwargs: {str(media_file): _FakeMedia()})
    transfer.progress = SimpleNamespace(start=lambda *args: None,
                                        update=lambda *args, **kwargs: None,
                                        end=lambda *args: None)
    transfer.dbhelper = SimpleNamespace(insert_transfer_history=lambda **kwargs: None)
    transfer.message = SimpleNamespace(send_transfer_fail_message=lambda *args: None)
    transfer.threadhelper = SimpleNamespace()
    transfer._ignored_paths = []
    transfer._ignored_files = ""
    transfer._min_filesize = 0
    transfer._filesize_cover = False
    transfer._movie_category_flag = False
    transfer._refresh_mediaserver = False
    transfer._scraper_flag = False
    transfer._FileTransfer__is_media_exists = lambda *args: (
        destination_exists, str(destination), False, str(destination / "fixture"))
    result = transfer.transfer_media(
        in_from=SyncType.MAN,
        in_path=str(source),
        rmt_mode=RmtMode.LINK,
        target_dir=str(target),
        media_type=MediaType.MOVIE,
        min_filesize="0",
        udf_flag=udf_flag,
    )
    return result, media_file, destination


def test_manual_transfer_reports_permission_failure_instead_of_false_success(tmp_path, monkeypatch):
    real_makedirs = filetransfer_module.os.makedirs

    def deny_destination(path, *args, **kwargs):
        if str(path).endswith("/recognized"):
            raise PermissionError("permission denied by fixture")
        return real_makedirs(path, *args, **kwargs)

    monkeypatch.setattr(filetransfer_module.os, "makedirs", deny_destination)
    result, source, destination = _transfer(tmp_path)

    assert result[0] is False
    assert "permission denied by fixture" in result[1]
    assert source.exists()
    assert not destination.exists()


def test_manual_transfer_reports_cross_device_hard_link_failure(tmp_path, monkeypatch):
    def fail_link(*args, **kwargs):
        return -1, "Invalid cross-device link"

    monkeypatch.setattr(filetransfer_module.SystemUtils, "link", fail_link)
    result, source, destination = _transfer(tmp_path, destination_exists=True)

    assert result == (False, "文件转移失败，错误码 -1")
    assert source.exists()
    assert not list(destination.glob("*.mkv"))


def test_directory_transfer_reports_permission_failure_instead_of_false_success(tmp_path, monkeypatch):
    real_makedirs = filetransfer_module.os.makedirs

    def deny_destination(path, *args, **kwargs):
        if str(path).endswith("/recognized"):
            raise PermissionError("permission denied by fixture")
        return real_makedirs(path, *args, **kwargs)

    monkeypatch.setattr(filetransfer_module.os, "makedirs", deny_destination)
    result, source, destination = _transfer(tmp_path, udf_flag=False)

    assert result[0] is False
    assert "permission denied by fixture" in result[1]
    assert source.exists()
    assert not destination.exists()
