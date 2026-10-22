"""S21 startup-only, fail-closed recognition schema upgrade coordinator."""

import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from alembic import command
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
import sqlalchemy as sa

from app.db import upgrade_backup as backups
from db_scripts.recognition_legacy import TABLES as RETIRED_TABLES


REVISION = "fc2f3a4b5c6d"
REMOTE_BASE = "fa0d1e2f3a4b"
V3_TABLES = ("RECOGNITION_TRACE", "RECOGNITION_EVENT", "RECOGNITION_CANDIDATE",
             "RECOGNITION_TASK", "RECOGNITION_OVERRIDE", "RECOGNITION_SECRET",
             "RECOGNITION_RECORD_STATE")


def _read(path):
    return json.loads(Path(path).read_text())


def _directory(path):
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def generation(fingerprint, root):
    path = Path(root) / ".maintenance" / "recognition-generation.json"
    try:
        state = _read(path)
        if not isinstance(state, dict) or not isinstance(state.get("epochs"), dict):
            return None
        epoch = state["epochs"].get(fingerprint)
        return f"{fingerprint}:{epoch}" if epoch else fingerprint
    except FileNotFoundError:
        return fingerprint
    except (OSError, ValueError, TypeError):
        return None  # corrupt control state must never permit old spool replay


def rotate_generation(fingerprint, root, operation):
    directory = _directory(Path(root) / ".maintenance")
    path = directory / "recognition-generation.json"
    try:
        state = _read(path)
    except FileNotFoundError:
        state = {"epochs": {}, "operations": {}}
    if not isinstance(state.get("epochs"), dict) or not isinstance(state.get("operations"), dict):
        raise RuntimeError("识别 generation 控制文件损坏，保持维护状态")
    if state["operations"].get(fingerprint) != operation:
        state["epochs"][fingerprint] = uuid.uuid4().hex
        state["operations"][fingerprint] = operation
        backups.write_json(path, state)
    return generation(fingerprint, root)


def _versions(engine):
    with engine.connect() as connection:
        return [row[0] for row in connection.execute(sa.text("SELECT version_num FROM alembic_version"))]


def _schema(engine):
    """Preserve columns/keys/indexes of every non-retired pre-existing table."""
    inspector = sa.inspect(engine)
    result = {}
    for name in inspector.get_table_names():
        if name in RETIRED_TABLES or name in V3_TABLES or name == "alembic_version":
            continue
        columns = [{"name": col["name"], "type": str(col["type"]), "nullable": col["nullable"],
                    "default": str(col.get("default"))} for col in inspector.get_columns(name)]
        result[name] = {"columns": columns, "pk": inspector.get_pk_constraint(name),
                        "indexes": inspector.get_indexes(name), "fk": inspector.get_foreign_keys(name),
                        "unique": inspector.get_unique_constraints(name)}
    # 统一转换元组、列表及数据库方言返回的其他标量容器，以便比较日志记录。
    return json.loads(json.dumps(result, default=str))


def verify_v3(connection, repair_indexes=False):
    from app.db.models import Base
    inspector = sa.inspect(connection)
    for name in V3_TABLES:
        table = Base.metadata.tables[name]
        if name not in inspector.get_table_names():
            raise RuntimeError("V3 结构不完整，禁止删除旧表")
        actual = {col["name"]: col for col in inspector.get_columns(name)}
        if set(actual) != set(table.columns.keys()):
            raise RuntimeError("V3 字段不匹配，禁止删除旧表")
        for column in table.columns:
            value = actual[column.name]
            if value["type"]._type_affinity is not column.type._type_affinity:
                raise RuntimeError("V3 字段类型不匹配，禁止删除旧表")
            if bool(value["nullable"]) != bool(column.nullable) and not column.primary_key:
                raise RuntimeError("V3 字段约束不匹配，禁止删除旧表")
            if getattr(column.type, "length", None) != getattr(value["type"], "length", None):
                raise RuntimeError("V3 字段长度不匹配，禁止删除旧表")
        if set(inspector.get_pk_constraint(name)["constrained_columns"]) != set(col.name for col in table.primary_key):
            raise RuntimeError("V3 主键不匹配，禁止删除旧表")
        expected_foreign_keys = {(tuple(element.parent.name for element in constraint.elements),
                                  constraint.referred_table.name,
                                  tuple(element.column.name for element in constraint.elements),
                                  (constraint.ondelete or "").upper())
                                 for constraint in table.foreign_key_constraints}
        actual_foreign_keys = {(tuple(foreign["constrained_columns"]), foreign["referred_table"],
                                tuple(foreign["referred_columns"]),
                                (foreign.get("options", {}).get("ondelete") or "").upper())
                               for foreign in inspector.get_foreign_keys(name)}
        if actual_foreign_keys != expected_foreign_keys:
            raise RuntimeError("V3 外键不匹配，禁止删除旧表")
        actual_indexes = {index["name"]: index for index in inspector.get_indexes(name)}
        for index in table.indexes:
            if index.name not in actual_indexes and repair_indexes:
                index.create(connection, checkfirst=True)
                continue
            found = actual_indexes.get(index.name)
            if (found is None or found["column_names"] != [col.name for col in index.columns]
                    or bool(found["unique"]) != bool(index.unique)):
                raise RuntimeError("V3 索引不匹配，禁止删除旧表")


def finalize_migration(connection, proof, checkpoint=lambda _stage: None):
    """Called only by the final Alembic revision with a verified backup proof."""
    names = set(sa.inspect(connection).get_table_names())
    if "MEDIASYNC_ITEMS" in names and "CONFIG_USERS" not in names:
        return  # media-only historical snapshots remain in their own domain
    if not proof or proof.get("connection") is not connection or not proof.get("verified"):
        raise RuntimeError("识别旧表删除必须通过已完成备份恢复校验的启动升级门禁")
    from app.db.models import Base
    for name in V3_TABLES:
        Base.metadata.tables[name].create(connection, checkfirst=True)
    verify_v3(connection, repair_indexes=True)
    checkpoint("v3_verified")
    inspector = sa.inspect(connection)
    for name in names - set(RETIRED_TABLES):
        for foreign_key in inspector.get_foreign_keys(name):
            if foreign_key["referred_table"] in RETIRED_TABLES:
                raise RuntimeError("非识别表引用待删除旧表，拒绝级联删除")
    for name in inspector.get_view_names():
        definition = str(inspector.get_view_definition(name) or "").upper()
        if any(retired in definition for retired in RETIRED_TABLES):
            raise RuntimeError("视图引用待删除旧表，需先明确转换方案")
    for name in RETIRED_TABLES:  # fixed child-to-parent allowlist, no CASCADE
        if name in set(sa.inspect(connection).get_table_names()):
            sa.Table(name, sa.MetaData(), autoload_with=connection).drop(connection)
        checkpoint("dropped:" + name)


def _key_backup(root, directory):
    external = os.environ.get("NASTOOL_RECOGNITION_MASTER_KEY")
    if external:
        return {"source": "environment", "message": "主密钥需独立备份并在恢复时重新提供；数据库备份不含环境变量"}
    path = Path(root) / ".recognition-master.key"
    if not path.exists():
        return {"source": "missing", "message": "如库内已有密钥密文，需恢复原主密钥或清除后重新配置 API Key"}
    if path.is_symlink() or path.stat().st_mode & 0o077 or path.stat().st_size != 32:
        raise RuntimeError("识别主密钥权限或格式无效，拒绝生成不可恢复的升级备份")
    target = directory / "recognition-master.key"
    with os.fdopen(os.open(target, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600), "wb") as stream:
        stream.write(path.read_bytes())
        stream.flush()
        os.fsync(stream.fileno())
    return {"source": "file", "file": target.name, "sha256": backups.sha256(target),
            "message": "回滚需同时恢复本目录主密钥至配置目录 .recognition-master.key，权限 0600"}


def upgrade_engine(engine, root, settings, *, target="head", active=True, checkpoint=lambda _stage: None):
    """Must run under the caller's exclusive storage lock before workers start.

    A failed job keeps a durable admission gate across process death. The next
    startup revalidates its original backup and resumes idempotent DDL.
    """
    from app.db.schema import current_schema_revision
    root = Path(root)
    script_location = Path(__file__).resolve().parents[2] / "db_scripts"
    config = AlembicConfig()
    config.set_main_option("script_location", str(script_location))
    scripts = ScriptDirectory.from_config(config)
    head = current_schema_revision(str(script_location)) if target == "head" else target
    versions = _versions(engine)
    if len(versions) != 1 or scripts.get_revision(versions[0]) is None:
        raise RuntimeError("数据库结构版本无效，拒绝升级")
    if engine.dialect.name != "sqlite" and versions[0] not in {REMOTE_BASE, "fb1e2f3a4b5c", REVISION}:
        raise RuntimeError("远程数据库结构版本不受支持，需先执行受控升级")
    maintenance = _directory(root / ".maintenance")
    gate_path = maintenance / "recognition-upgrade-gate.json"
    identity = hashlib.sha256(engine.url.render_as_string(hide_password=True).encode()).hexdigest()
    journal_root = _directory(maintenance / "recognition-upgrade" / identity)
    pointer = journal_root / "active.json"
    state = _read(pointer) if pointer.exists() else None
    if state is not None and (not isinstance(state, dict)
                              or not re.fullmatch(r"[a-f0-9]{32}", str(state.get("job_id", "")))
                              or state.get("identity") != identity):
        raise RuntimeError("识别升级日志损坏或归属错误，保持维护状态")
    # 复制或恢复的旧数据库要开始新的备份周期，不能覆盖原回滚快照，也不能复用已完成的校验记录。
    pending = bool(state and state.get("phase") != "complete")
    if versions == [head] and not pending:
        if set(RETIRED_TABLES) & set(sa.inspect(engine).get_table_names()):
            raise RuntimeError("数据库已标记当前版本但仍含旧识别表，拒绝静默删除")
        if active and gate_path.exists():
            gate = _read(gate_path)
            from app.media.recognition.admission import process_alive
            if (state and state.get("phase") == "complete" and gate.get("identity") == identity
                    and (gate.get("pid") == os.getpid()
                         or not process_alive(gate.get("pid"), gate.get("process_start")))):
                with engine.connect() as connection:
                    verify_v3(connection)
                gate_path.unlink()
                backups.sync_directory(maintenance)
            else:
                raise RuntimeError("存在未完成的其他识别升级门禁，拒绝恢复业务")
        return None
    # 历史中间版本不会执行到具有破坏性的迁移版本。
    needs_final = head == REVISION and versions[0] != REVISION
    if not needs_final and not pending:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, head)
        return None
    if active:
        from app.media.recognition.admission import gate, process_alive, process_start
        existing = gate(root)
        if existing and existing.get("reason") != "recognition_upgrade":
            raise RuntimeError("识别维护尚未结束，拒绝并发结构升级")
        if existing and (existing.get("identity") != identity
                         or (existing.get("pid") != os.getpid()
                             and process_alive(existing.get("pid"), existing.get("process_start")))):
            raise RuntimeError("识别升级门禁属于其他数据库或存活进程")
        backups.write_json(gate_path, {"reason": "recognition_upgrade", "identity": identity,
                                      "pid": os.getpid(), "process_start": process_start(os.getpid())})
    if not pending:
        job_id = uuid.uuid4().hex
        state = {"job_id": job_id, "source_revision": versions[0], "target_revision": head,
                 "identity": identity, "phase": "backing_up"}
        backups.write_json(pointer, state)
    elif state.get("target_revision") != head:
        raise RuntimeError("未完成升级的目标版本不匹配，保持维护状态")
    directory = _directory(journal_root / state["job_id"])

    def save(phase):
        state["phase"] = phase
        backups.write_json(directory / "manifest.json", state)
        backups.write_json(pointer, state)
        checkpoint(phase)

    if state["phase"] == "backing_up":
        state["backup"] = backups.create(engine, settings, directory)
        state["key"] = _key_backup(root, directory)
        save("backup_created")
    # 每次重试都要重新校验，包括 Alembic 已提交后进程崩溃的情况。
    backups.verify(engine, settings, directory, state["backup"],
                   "nastools_upgrade_check_" + state["job_id"])
    save("backup_verified")
    if "preservation_digests" not in state:
        # 旧版 SQLite 迁移包含有意进行的业务结构变更。
        # 保留原生备份以供回滚，先执行旧迁移链，再在 V3 增量及最终迁移开始前固定数据保全边界。
        # 远程数据库仍禁止使用仅适用于 SQLite 的迁移路径。
        if _versions(engine)[0] not in {REMOTE_BASE, "fb1e2f3a4b5c", REVISION}:
            with engine.connect() as connection:
                config.attributes["connection"] = connection
                command.upgrade(config, REMOTE_BASE)
        state["preserved_schema"] = _schema(engine)
        state["preservation_digests"] = {
            name: value for name, value in backups.digests(engine).items()
            if name not in RETIRED_TABLES and name != "alembic_version"}
        save("baseline_verified")
    if _versions(engine) != [head]:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            config.attributes["recognition_upgrade_verified"] = {"connection": connection, "verified": True}
            config.attributes["recognition_upgrade_checkpoint"] = checkpoint
            command.upgrade(config, head)
    save("schema_upgraded")
    with engine.connect() as connection:
        verify_v3(connection)
    if _versions(engine) != [head] or set(RETIRED_TABLES) & set(sa.inspect(engine).get_table_names()):
        raise RuntimeError("识别结构升级后版本或旧表检查失败，保持维护状态")
    expected = state["preservation_digests"]
    if backups.digests(engine, expected) != expected:
        raise RuntimeError("升级后原有业务/覆盖/密钥/新识别记录不一致，保持维护状态")
    current_schema = _schema(engine)
    if any(current_schema.get(name) != value for name, value in state["preserved_schema"].items()):
        raise RuntimeError("升级后原有表结构不一致，保持维护状态")
    save("data_verified")
    if active:
        from app.db.maintenance import _retire_recognition_spool
        # 代次变更前不要重放升级前保存的识别缓冲数据。
        _retire_recognition_spool(state["job_id"], str(root))
        from app.db.migration import _fingerprint
        rotate_generation(_fingerprint(settings), root, "upgrade:" + state["job_id"])
    save("spool_retired")
    save("complete")
    if active:
        gate_path.unlink(missing_ok=True)
        backups.sync_directory(maintenance)
    return directory
