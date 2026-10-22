import regex as re
from types import SimpleNamespace

from app.helper import DbHelper
from app.utils.commons import singleton
from app.utils.word_rules import WordRuleProcessor


@singleton
class WordsHelper(WordRuleProcessor):
    dbhelper = None
    ignored_words_info = []
    ignored_words_noregex_info = []
    replaced_words_info = []
    replaced_words_noregex_info = []
    replaced_offset_words_info = []
    offset_words_info = []

    def __init__(self):
        self.init_config()

    def init_config(self):
        self.dbhelper = DbHelper()

        def load_rules(**filters):
            # WordsHelper 是进程级单例，而查询结果属于线程级数据库会话。
            # 复制为普通值，避免后续工作线程访问已脱离会话的 ORM 对象。
            return [SimpleNamespace(
                ID=word.ID,
                REPLACED=word.REPLACED,
                REPLACE=word.REPLACE,
                FRONT=word.FRONT,
                BACK=word.BACK,
                OFFSET=word.OFFSET,
            ) for word in self.dbhelper.get_custom_words(**filters)]

        self.ignored_words_info = load_rules(enabled=1, wtype=1, regex=1)
        self.ignored_words_noregex_info = load_rules(enabled=1, wtype=1, regex=0)
        self.replaced_words_info = load_rules(enabled=1, wtype=2, regex=1)
        self.replaced_words_noregex_info = load_rules(enabled=1, wtype=2, regex=0)
        self.replaced_offset_words_info = load_rules(enabled=1, wtype=3, regex=1)
        self.offset_words_info = load_rules(enabled=1, wtype=4, regex=1)
