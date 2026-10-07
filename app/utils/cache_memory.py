"""估算缓存对象占用；共享引用只计算一次，不遍历解释器全局对象。"""

import sys
from collections import deque
from enum import Enum
from types import BuiltinFunctionType, FunctionType, MemberDescriptorType, MethodType, ModuleType

_LEAF_TYPES = (str, bytes, bytearray, int, float, complex, bool, type(None), range,
               type, Enum, ModuleType, FunctionType, BuiltinFunctionType, MethodType)


def estimate_memory(*values):
    pending = list(values)
    seen = set()
    total = 0
    while pending:
        value = pending.pop()
        identity = id(value)
        if identity in seen:
            continue
        seen.add(identity)
        total += sys.getsizeof(value, 0)
        if isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, (list, tuple, set, frozenset, deque)):
            pending.extend(value)
        elif isinstance(value, memoryview):
            pending.append(value.obj)
        elif not isinstance(value, _LEAF_TYPES):
            attributes = getattr(value, "__dict__", None)
            if isinstance(attributes, dict):
                pending.append(attributes)
            for cls in type(value).__mro__:
                for name, descriptor in vars(cls).items():
                    if isinstance(descriptor, MemberDescriptorType):
                        try:
                            pending.append(getattr(value, name))
                        except AttributeError:
                            pass
    return total


def cache_memory_info(cache):
    with cache._lock:
        cache.delete_expired()
        return {"entries": len(cache),
                "memory_bytes": estimate_memory(cache._cache, cache._expire_times)
                if cache._cache else 0}
