"""将识别列表筛选维度从大 JSON 投影到可索引字段。"""

import hashlib
import json

import sqlalchemy as sa
from alembic import op


revision = "f4a5b6c7d8e9"
down_revision = "f3a4b5c6d7e8"
branch_labels = None
depends_on = None


_REQUEST_TABLE = "RECOGNITION_REQUEST"
_ACTION_TABLE = "RECOGNITION_REQUEST_ACTION"
_COLUMNS = {
    "STATUS_SUMMARY": sa.String(64),
    "BUSINESS_STATUS_SUMMARY": sa.String(64),
    "REASON_SUMMARY": sa.Text(),
    "REASON_HASH": sa.String(64),
    "BUSINESS_REASON_SUMMARY": sa.Text(),
    "BUSINESS_REASON_HASH": sa.String(64),
}


def _value(value):
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


def _status(value):
    value = _value(value)
    if value is None:
        return None
    value = value.casefold()
    if len(value) > 64:
        raise RuntimeError("旧识别状态超过 64 个字符，拒绝截断迁移")
    return value


def _reason(value):
    value = _value(value)
    if value is None:
        return None, None
    return value, _digest(value)


def _digest(*values):
    digest = hashlib.sha256()
    for value in values:
        encoded = str(value).encode("utf-8", "surrogatepass")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _json_object(value):
    if value is None:
        return {}
    if not isinstance(value, str):
        if isinstance(value, dict):
            return value
        raise RuntimeError("旧识别结果 JSON 结构异常，拒绝静默丢失筛选数据")
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as error:
        raise RuntimeError("旧识别结果 JSON 无法解析，拒绝静默丢失筛选数据") from error
    if not isinstance(parsed, dict):
        raise RuntimeError("旧识别结果 JSON 结构异常，拒绝静默丢失筛选数据")
    return parsed


def _action_types(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, json.JSONDecodeError) as error:
            raise RuntimeError("旧识别动作 JSON 无法解析，拒绝静默丢失筛选数据") from error
    if value == {}:
        return set()
    if value is None:
        return set()
    if not isinstance(value, list):
        raise RuntimeError("旧识别动作 JSON 结构异常，拒绝静默丢失筛选数据")
    result = set()
    for item in value:
        if not isinstance(item, dict) or item.get("action_type") is None:
            raise RuntimeError("旧识别动作缺少动作类型，无法建立筛选索引")
        action = str(item["action_type"]).casefold()
        if len(action) > 256:
            raise RuntimeError("旧识别动作类型超过 256 个字符，拒绝截断迁移")
        result.add(action)
    return result


def _projection(overall_json, actions_json):
    overall = _json_object(overall_json)
    business = overall.get("business_result")
    business = business if isinstance(business, dict) else {}
    reason, reason_hash = _reason(overall.get("reason"))
    business_reason, business_reason_hash = _reason(business.get("reason"))
    return ({
        "STATUS_SUMMARY": _status(overall.get("status")),
        "BUSINESS_STATUS_SUMMARY": _status(business.get("status")),
        "REASON_SUMMARY": reason,
        "REASON_HASH": reason_hash,
        "BUSINESS_REASON_SUMMARY": business_reason,
        "BUSINESS_REASON_HASH": business_reason_hash,
    }, _action_types(actions_json))


def upgrade():
    bind = op.get_bind()
    tables = {name.upper() for name in sa.inspect(bind).get_table_names()}
    # The old media-only snapshot is also advanced through this Alembic head,
    # but it has no recognition domain to migrate.
    if _REQUEST_TABLE not in tables:
        return

    existing_columns = {column["name"].upper() for column in
                        sa.inspect(bind).get_columns(_REQUEST_TABLE)}
    for name, column_type in _COLUMNS.items():
        if name not in existing_columns:
            op.add_column(_REQUEST_TABLE, sa.Column(name, column_type, nullable=True))

    tables = {name.upper() for name in sa.inspect(bind).get_table_names()}
    if _ACTION_TABLE not in tables:
        op.create_table(
            _ACTION_TABLE,
            sa.Column("ID", sa.String(64), primary_key=True),
            sa.Column("REQUEST_ID", sa.String(128), nullable=False),
            sa.Column("ACTION_TYPE", sa.String(256), nullable=False),
            sa.Column("ACTION_HASH", sa.String(64), nullable=False),
        )

    indexes = {index["name"] for index in sa.inspect(bind).get_indexes(_REQUEST_TABLE)}
    for name, column in (
        ("IDX_RECOG_STATUS_SUMMARY", "STATUS_SUMMARY"),
        ("IDX_RECOG_BUSINESS_STATUS", "BUSINESS_STATUS_SUMMARY"),
        ("IDX_RECOG_REASON_HASH", "REASON_HASH"),
        ("IDX_RECOG_BUSINESS_REASON", "BUSINESS_REASON_HASH"),
    ):
        if name not in indexes:
            op.create_index(name, _REQUEST_TABLE, [column])
    action_indexes = {index["name"] for index in sa.inspect(bind).get_indexes(_ACTION_TABLE)}
    if "IDX_RECOG_ACTION_HASH" not in action_indexes:
        op.create_index("IDX_RECOG_ACTION_HASH", _ACTION_TABLE, ["ACTION_HASH"])
    if "IDX_RECOG_ACTION_REQUEST" not in action_indexes:
        op.create_index("IDX_RECOG_ACTION_REQUEST", _ACTION_TABLE, ["REQUEST_ID"])

    request = sa.Table(_REQUEST_TABLE, sa.MetaData(), autoload_with=bind)
    action = sa.Table(_ACTION_TABLE, sa.MetaData(), autoload_with=bind)
    update_statement = sa.update(request).where(
        request.c.REQUEST_ID == sa.bindparam("_request_id")).values(
        **{name: sa.bindparam(f"_{name.lower()}") for name in _COLUMNS})
    last_request_id = None
    while True:
        statement = sa.select(request.c.REQUEST_ID, request.c.OVERALL_RESULT,
                              request.c.ACTIONS).order_by(request.c.REQUEST_ID).limit(500)
        if last_request_id is not None:
            statement = statement.where(request.c.REQUEST_ID > last_request_id)
        rows = bind.execute(statement).all()
        if not rows:
            break
        updates = []
        request_ids = []
        action_rows = []
        for row in rows:
            request_id = row[0]
            projection, action_types = _projection(row[1], row[2])
            updates.append({"_request_id": request_id,
                            **{f"_{name.lower()}": value
                               for name, value in projection.items()}})
            request_ids.append(request_id)
            action_rows.extend({
                "ID": _digest(request_id, action_type),
                "REQUEST_ID": request_id,
                "ACTION_TYPE": action_type,
                "ACTION_HASH": _digest(action_type),
            } for action_type in action_types)
        action_identities = {}
        for item in action_rows:
            identity = (item["REQUEST_ID"], item["ACTION_TYPE"])
            previous = action_identities.setdefault(item["ID"], identity)
            if previous != identity:
                raise RuntimeError("识别动作摘要冲突，拒绝覆盖不同记录")
        last_request_id = rows[-1][0]
        if updates:
            bind.execute(update_statement, updates)
        if action_rows:
            existing = {row[0]: (row[1], row[2]) for row in bind.execute(
                sa.select(action.c.ID, action.c.REQUEST_ID, action.c.ACTION_TYPE).where(
                    action.c.ID.in_([item["ID"] for item in action_rows])))}
            missing = []
            for item in action_rows:
                current = existing.get(item["ID"])
                if current and current != (item["REQUEST_ID"], item["ACTION_TYPE"]):
                    raise RuntimeError("识别动作摘要冲突，拒绝覆盖不同记录")
                if not current:
                    missing.append(item)
            if missing:
                bind.execute(sa.insert(action), missing)
def downgrade():
    bind = op.get_bind()
    tables = {name.upper() for name in sa.inspect(bind).get_table_names()}
    if _ACTION_TABLE in tables:
        indexes = {item["name"] for item in sa.inspect(bind).get_indexes(_ACTION_TABLE)}
        for name in ("IDX_RECOG_ACTION_HASH", "IDX_RECOG_ACTION_REQUEST"):
            if name in indexes:
                op.drop_index(name, table_name=_ACTION_TABLE)
        op.drop_table(_ACTION_TABLE)
    if _REQUEST_TABLE not in tables:
        return
    indexes = {item["name"] for item in sa.inspect(bind).get_indexes(_REQUEST_TABLE)}
    for name in ("IDX_RECOG_STATUS_SUMMARY", "IDX_RECOG_BUSINESS_STATUS",
                 "IDX_RECOG_REASON_HASH", "IDX_RECOG_BUSINESS_REASON"):
        if name in indexes:
            op.drop_index(name, table_name=_REQUEST_TABLE)
    columns = {column["name"].upper() for column in
               sa.inspect(bind).get_columns(_REQUEST_TABLE)}
    for name in reversed(list(_COLUMNS)):
        if name in columns:
            op.drop_column(_REQUEST_TABLE, name)
