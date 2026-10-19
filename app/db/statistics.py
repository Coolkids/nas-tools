"""数据库精确统计定时任务配置，与数据库连接设置分开保存。"""


def database_statistics_schedule(settings=None):
    """仅校验定时配置，默认启用每 12 小时统计；手动刷新不调用此校验。"""
    if settings is None:
        settings = {}
    if not isinstance(settings, dict):
        raise ValueError("数据库精确统计配置必须为对象")
    enabled = settings.get("enabled", True)
    if not isinstance(enabled, bool):
        normalized = str(enabled).strip().lower()
        if normalized in ("true", "1", "yes", "on"):
            enabled = True
        elif normalized in ("false", "0", "no", "off"):
            enabled = False
        else:
            raise ValueError("数据库精确统计开关无效")
    value = settings.get("interval_value", 12)
    unit = settings.get("interval_unit", "hour")
    if not str(value).strip().isdigit() or int(value) < 1:
        raise ValueError("数据库精确统计间隔必须为正整数")
    if unit not in ("hour", "day"):
        raise ValueError("数据库精确统计间隔单位必须为小时或天")
    value = int(value)
    hours = value * (24 if unit == "day" else 1)
    if hours < 12:
        raise ValueError("数据库精确统计间隔不能低于 12 小时")
    return {"enabled": enabled, "interval_value": value, "interval_unit": unit,
            "interval_hours": hours}
