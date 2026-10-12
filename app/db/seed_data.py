"""结构化的内置数据库种子数据读取。"""

import os
import sqlite3


def builtin_filter_groups():
    """读取随应用发布的过滤规则模板，不向业务数据库执行模板 SQL。"""
    from config import Config

    path = os.path.join(Config().get_root_path(), "config", "init_filter.sql")
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript("""
            CREATE TABLE CONFIG_FILTER_GROUP (
                ID INTEGER PRIMARY KEY, GROUP_NAME TEXT, IS_DEFAULT TEXT, NOTE TEXT
            );
            CREATE TABLE CONFIG_FILTER_RULES (
                ID INTEGER PRIMARY KEY, GROUP_ID TEXT, ROLE_NAME TEXT, PRIORITY TEXT,
                INCLUDE TEXT, EXCLUDE TEXT, SIZE_LIMIT TEXT, NOTE TEXT
            );
        """)
        with open(path, "r", encoding="utf-8") as stream:
            connection.executescript(stream.read())
        groups = []
        group_rows = connection.execute(
            "SELECT ID, GROUP_NAME, IS_DEFAULT, NOTE "
            "FROM CONFIG_FILTER_GROUP ORDER BY ID").fetchall()
        for group_id, name, default, note in group_rows:
            rules = connection.execute("""
                SELECT ID, ROLE_NAME, PRIORITY, INCLUDE, EXCLUDE, SIZE_LIMIT, NOTE
                FROM CONFIG_FILTER_RULES WHERE GROUP_ID=? ORDER BY ID
            """, (str(group_id),)).fetchall()
            groups.append({
                "id": int(group_id),
                "name": name,
                "default": default,
                "note": note,
                "rules": [{
                    "id": int(rule[0]), "name": rule[1], "priority": rule[2],
                    "include": rule[3], "exclude": rule[4], "size_limit": rule[5],
                    "note": rule[6],
                } for rule in rules],
            })
        return groups
    finally:
        connection.close()
