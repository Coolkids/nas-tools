import regex as re

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
        self.ignored_words_info = self.dbhelper.get_custom_words(enabled=1, wtype=1, regex=1)
        self.ignored_words_noregex_info = self.dbhelper.get_custom_words(enabled=1, wtype=1, regex=0)
        self.replaced_words_info = self.dbhelper.get_custom_words(enabled=1, wtype=2, regex=1)
        self.replaced_words_noregex_info = self.dbhelper.get_custom_words(enabled=1, wtype=2, regex=0)
        self.replaced_offset_words_info = self.dbhelper.get_custom_words(enabled=1, wtype=3, regex=1)
        self.offset_words_info = self.dbhelper.get_custom_words(enabled=1, wtype=4, regex=1)
