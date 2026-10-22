"""兼容旧业务表单传入的年份、季号，并保持内部数值约定。"""


def normalize_numeric_override(value, field):
    minimum, maximum = {"year": (1870, 2200), "season": (0, 1000)}[field]
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        # 只接受明确的十进制整数，不静默截断小数或将布尔值当作季号。
        if not value.isascii() or not value.isdecimal() or len(value) > 16:
            raise ValueError(f"invalid {field} override")
        value = int(value)
    if value is not None and (type(value) is not int or not minimum <= value <= maximum):
        raise ValueError(f"invalid {field} override")
    return value
