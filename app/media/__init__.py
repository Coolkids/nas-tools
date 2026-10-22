"""Load public clients on demand so pure recognition DTOs stay independent."""

from importlib import import_module

_EXPORTS = {"Category": ".category", "Media": ".media", "Scraper": ".scraper",
            "DouBan": ".douban", "Bangumi": ".bangumi"}
__all__ = list(_EXPORTS)


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(name)
    value = getattr(import_module(_EXPORTS[name], __name__), name)
    globals()[name] = value
    return value
