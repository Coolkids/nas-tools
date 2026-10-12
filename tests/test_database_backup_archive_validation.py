"""完整数据库备份 ZIP 的路径、结构、大小和摘要校验。"""

import json
import stat
import warnings
import zipfile

import pytest
from sqlalchemy import create_engine

import app.db.maintenance as maintenance
from app.db.models import Base


class _NoopJobs:
    @staticmethod
    def cancel_requested(_job_id):
        return False

    @staticmethod
    def update(*_args, **_kwargs):
        return None


def _entries(path):
    with zipfile.ZipFile(path, "r") as archive:
        return [(info.filename, archive.read(info), info.external_attr)
                for info in archive.infolist()]


def _write_archive(path, entries, compression=zipfile.ZIP_DEFLATED):
    with zipfile.ZipFile(path, "w", compression=compression) as archive:
        for name, payload, external_attr in entries:
            info = zipfile.ZipInfo(name)
            info.external_attr = external_attr
            archive.writestr(info, payload)


def _manifest_entry(entries):
    return next(index for index, (name, _payload, _attr) in enumerate(entries)
                if name == "manifest.json")


def test_backup_archive_validator_rejects_malformed_and_hostile_archives(
        tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, "STORAGE_JOBS", _NoopJobs())
    engine = create_engine(f"sqlite:///{tmp_path / 'source.sqlite3'}")
    archive_path = tmp_path / "valid.zip"
    try:
        Base.metadata.create_all(engine)
        with engine.connect() as connection:
            maintenance._export_backup(connection, str(archive_path), "validation", "sqlite")
        assert maintenance._validate_backup_archive(str(archive_path))["manifest"]
    finally:
        engine.dispose()

    entries = _entries(archive_path)
    missing_manifest = json.loads(entries[_manifest_entry(entries)][1])
    missing_manifest.pop("logical_domains")
    missing_entries = list(entries)
    name, _payload, attr = missing_entries[_manifest_entry(entries)]
    missing_entries[_manifest_entry(entries)] = (
        name, json.dumps(missing_manifest).encode(), attr)

    cases = [
        (entries + [("../outside.jsonl", b"x", 0)], "非法路径"),
        (entries + [("extra.jsonl", b"x", (stat.S_IFLNK | 0o777) << 16)], "符号链接"),
        (entries + [entries[_manifest_entry(entries)]], "重复文件名"),
        (entries + [("extra.jsonl", b"x", 0)], "未知文件"),
        ([entry for entry in entries if entry[0] != "manifest.json"], "缺少 manifest.json"),
        (missing_entries, "逻辑数据域"),
    ]
    for index, (invalid_entries, message) in enumerate(cases):
        invalid_path = tmp_path / f"invalid-{index}.zip"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            _write_archive(invalid_path, invalid_entries)
        with pytest.raises((ValueError, KeyError), match=message):
            maintenance._validate_backup_archive(str(invalid_path))

    future_manifest = json.loads(entries[_manifest_entry(entries)][1])
    future_manifest["format_version"] = 999
    future_entries = list(entries)
    name, _payload, attr = future_entries[_manifest_entry(entries)]
    future_entries[_manifest_entry(entries)] = (
        name, json.dumps(future_manifest).encode(), attr)
    future_path = tmp_path / "future.zip"
    _write_archive(future_path, future_entries)
    with pytest.raises(ValueError, match="受支持的完整数据库备份"):
        maintenance._validate_backup_archive(str(future_path))

    bad_digest = json.loads(entries[_manifest_entry(entries)][1])
    first_table = next(iter(bad_digest["tables"]))
    bad_digest["tables"][first_table]["sha256"] = "0" * 64
    digest_entries = list(entries)
    name, _payload, attr = digest_entries[_manifest_entry(entries)]
    digest_entries[_manifest_entry(entries)] = (
        name, json.dumps(bad_digest).encode(), attr)
    digest_path = tmp_path / "digest.zip"
    _write_archive(digest_path, digest_entries)
    with pytest.raises(ValueError, match="SHA-256 校验失败"):
        maintenance._validate_backup_archive(str(digest_path))

    bomb_path = tmp_path / "ratio.zip"
    _write_archive(bomb_path, entries, compression=zipfile.ZIP_BZIP2)
    with zipfile.ZipFile(bomb_path, "a", compression=zipfile.ZIP_BZIP2) as archive:
        archive.writestr("ratio.bin", bytes(1024 * 1024))
    with pytest.raises(ValueError, match="压缩比异常"):
        maintenance._validate_backup_archive(str(bomb_path))

    with monkeypatch.context() as patcher:
        patcher.setattr(maintenance, "_MAX_ARCHIVE_ENTRIES", 1)
        with pytest.raises(ValueError, match="条目过多"):
            maintenance._validate_backup_archive(str(archive_path))
    with monkeypatch.context() as patcher:
        patcher.setattr(maintenance, "_MAX_MANIFEST_BYTES", 4)
        with pytest.raises(ValueError, match="清单超过允许大小"):
            maintenance._validate_backup_archive(str(archive_path))
    with monkeypatch.context() as patcher:
        patcher.setattr(maintenance, "_MAX_ARCHIVE_BYTES", 1)
        with pytest.raises(ValueError, match="解压后备份体积"):
            maintenance._validate_backup_archive(str(archive_path))

    corrupt_path = tmp_path / "corrupt.zip"
    corrupt_path.write_bytes(b"not a zip file")
    with pytest.raises(zipfile.BadZipFile):
        maintenance._validate_backup_archive(str(corrupt_path))
