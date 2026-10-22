"""Private, native pre-upgrade snapshots, verified by an actual isolated restore.

These snapshots retain the *old* schema for binary rollback. They are deliberately
separate from the current-schema portable backup/upload API.
"""

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess

import sqlalchemy as sa


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(value, stream, ensure_ascii=False, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    sync_directory(path.parent)


def digests(engine, names=None):
    """Order-independent, duplicate-sensitive row hashes; bounded memory.

    Native restore keeps DBAPI value types. Avoid reflected type conversion,
    which could silently change date/text/binary representations.
    """
    from app.db.maintenance import _encode_value
    result = {}
    with engine.connect() as connection:
        inspector = sa.inspect(connection)
        available = set(inspector.get_table_names())
        for name in sorted(available if names is None else names):
            if name not in available:
                raise RuntimeError("升级校验缺少原有表")
            quote = connection.dialect.identifier_preparer.quote
            rows = connection.exec_driver_sql("SELECT * FROM " + quote(name),
                                               execution_options={"stream_results": True})
            count = total = 0
            for row in rows:
                wire = json.dumps([_encode_value(value) for value in row],
                                  ensure_ascii=False, sort_keys=True, allow_nan=False,
                                  separators=(",", ":")).encode("utf-8", "surrogatepass")
                total = (total + int.from_bytes(hashlib.sha256(wire).digest(), "big")) % (1 << 256)
                count += 1
            rows.close()
            result[name] = {"rows": count, "sha256_sum": f"{total:064x}"}
    return result


def structure(engine):
    inspector = sa.inspect(engine)
    result = {"tables": {}, "views": {}}
    for name in sorted(inspector.get_table_names()):
        result["tables"][name] = {
            "columns": [{key: str(value) if key == "type" else value
                         for key, value in col.items()} for col in inspector.get_columns(name)],
            "primary_key": inspector.get_pk_constraint(name), "indexes": inspector.get_indexes(name),
            "foreign_keys": inspector.get_foreign_keys(name), "unique": inspector.get_unique_constraints(name),
        }
    for name in inspector.get_view_names():
        result["views"][name] = inspector.get_view_definition(name)
    return json.loads(json.dumps(result, default=str))


def _run(arguments, *, env=None, source=None, target=None):
    try:
        subprocess.run(arguments, env=env, stdin=source, stdout=target or subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=600, check=True)
    except (OSError, subprocess.SubprocessError):
        # 原生客户端的诊断信息可能包含连接详情或行数据。
        raise RuntimeError("升级原生备份/恢复校验失败；检查客户端版本、TLS及临时校验库权限") from None


def _tool(*names):
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    raise RuntimeError("升级需要原生备份与恢复客户端：" + "/".join(names))


def _remote_tools(settings, directory):
    """Passwords never appear in argv, reports or application logs."""
    env = os.environ.copy()
    if settings["type"] == "postgresql":
        env.update(PGHOST=str(settings["host"]), PGPORT=str(settings.get("port") or 5432),
                   PGUSER=str(settings["username"]), PGPASSWORD=str(settings.get("password") or ""))
        ssl = settings.get("ssl") or {}
        if ssl.get("mode"):
            env["PGSSLMODE"] = str(ssl["mode"])
        return (_tool("pg_dump"), _tool("pg_restore"), [], env)
    # defaults-extra-file 必须放在其他 MySQL/MariaDB 参数之前。
    options = {"host": settings["host"], "port": settings.get("port") or 3306,
               "user": settings["username"], "password": settings.get("password") or "",
               "protocol": "TCP", "default-character-set": "utf8mb4"}
    tls = settings.get("ssl") or {}
    mapping = {"ca": "ssl-ca", "cert": "ssl-cert", "key": "ssl-key"}
    if set(tls) - set(mapping):
        raise RuntimeError("原生升级备份不支持该 MySQL TLS 配置，需先提供受控备份方案")
    options.update({mapping[key]: value for key, value in tls.items()})
    path = Path(directory) / ".client.cnf"
    with os.fdopen(os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600), "w") as stream:
        stream.write("[client]\n")
        for key, value in options.items():
            quoted = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
            stream.write(f'{key}="{quoted}"\n')
    maria = settings["type"] == "mariadb"
    return (_tool("mariadb-dump" if maria else "mysqldump"),
            _tool("mariadb" if maria else "mysql"), [f"--defaults-extra-file={path}"], env)


def create(engine, settings, directory):
    directory = Path(directory)
    path = directory / ("before.sqlite3" if engine.dialect.name == "sqlite" else "before.dump")
    temporary = path.with_suffix(".partial")
    if engine.dialect.name == "sqlite" and engine.url.database and engine.url.database != ":memory:":
        source_size = sum(os.path.getsize(engine.url.database + suffix)
                          for suffix in ("", "-wal") if os.path.isfile(engine.url.database + suffix))
        if shutil.disk_usage(directory).free < source_size * 2 + 64 * 1024 * 1024:
            raise RuntimeError("升级目录空间不足，无法完成备份和实际恢复校验")
    before = digests(engine)
    schema = structure(engine)
    try:
        if engine.dialect.name == "sqlite":
            with engine.connect() as connection, sqlite3.connect(temporary) as target:
                connection.connection.driver_connection.backup(target)
        else:
            dump, _restore, arguments, env = _remote_tools(settings, directory)
            arguments = [dump, *arguments]
            if settings["type"] == "postgresql":
                arguments += ["--format=custom", "--dbname=" + settings["name"]]
            else:
                arguments += ["--single-transaction", "--routines", "--events", "--hex-blob", settings["name"]]
            with os.fdopen(os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600), "wb") as target:
                _run(arguments, env=env, target=target)
        os.chmod(temporary, 0o600)
        with open(temporary, "rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(directory)
        if digests(engine) != before:
            raise RuntimeError("备份期间数据库内容变化，拒绝继续升级")
        return {"file": path.name, "sha256": sha256(path), "digests": before, "schema": schema}
    finally:
        (directory / ".client.cnf").unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)


def verify(engine, settings, directory, backup, check_name):
    """Restore all tables into an owned temporary database, compare every row."""
    directory = Path(directory)
    path = directory / backup["file"]
    if path.parent != directory or sha256(path) != backup["sha256"]:
        raise RuntimeError("升级前备份文件校验失败，拒绝继续")
    restored = None
    created = False
    clone = directory / "restore-check.sqlite3"
    try:
        if engine.dialect.name == "sqlite":
            shutil.copyfile(path, clone)
            os.chmod(clone, 0o600)
            restored = sa.create_engine(sa.URL.create("sqlite", database=str(clone)))
            with restored.connect() as connection:
                if connection.exec_driver_sql("PRAGMA integrity_check").scalar_one() != "ok":
                    raise RuntimeError("升级前备份恢复完整性校验失败")
                if connection.exec_driver_sql("PRAGMA foreign_key_check").first():
                    raise RuntimeError("升级前备份存在外键错误，拒绝删除历史表")
        else:
            # 名称由私有升级日志中的 UUID 派生，不能使用用户输入。
            import re
            if not re.fullmatch(r"nastools_upgrade_check_[a-f0-9]{32}", check_name):
                raise RuntimeError("升级恢复校验库标识无效")
            dump, restore, arguments, env = _remote_tools(settings, directory)
            quote = engine.dialect.identifier_preparer.quote
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
                # 上一个进程可能在此任务自身的校验过程中意外退出。
                connection.exec_driver_sql("DROP DATABASE IF EXISTS " + quote(check_name))
                suffix = " TEMPLATE template0" if settings["type"] == "postgresql" else " CHARACTER SET utf8mb4"
                connection.exec_driver_sql("CREATE DATABASE " + quote(check_name) + suffix)
                created = True
            from app.db.backends import create_backend
            restored = create_backend(settings["type"]).create_engine({**settings, "name": check_name})
            if settings["type"] == "postgresql":
                _run([restore, "--exit-on-error", "--no-owner", "--no-acl", "--dbname=" + check_name, str(path)], env=env)
            else:
                with open(path, "rb") as source:
                    _run([restore, *arguments, check_name], env=env, source=source)
        if digests(restored) != backup["digests"]:
            raise RuntimeError("升级前备份实际恢复后的逐表数据校验失败")
        if structure(restored) != backup["schema"]:
            raise RuntimeError("升级前备份实际恢复后的表/索引/视图结构校验失败")
    finally:
        if restored is not None:
            restored.dispose()
        clone.unlink(missing_ok=True)
        (directory / ".client.cnf").unlink(missing_ok=True)
        if created:
            quote = engine.dialect.identifier_preparer.quote
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
                connection.exec_driver_sql("DROP DATABASE " + quote(check_name))
