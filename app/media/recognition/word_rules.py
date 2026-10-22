"""为不依赖外部库的自定义词语操作提供兼容性导出。"""

from app.utils.word_rules import WordRuleProcessor, offset_value

__all__ = ["WordRuleProcessor", "offset_value"]
