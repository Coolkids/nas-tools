# coding: utf-8
import hashlib

from sqlalchemy import BigInteger, Column, Float, Index, Integer, String, Text, event, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import deferred
from sqlalchemy.dialects.mysql import LONGTEXT

MEDIA_SYNC_LEGACY_GENERATION = "legacy"

Base = declarative_base()
# 两个业务逻辑域共用一份 metadata 和结构版本。
BaseMedia = declarative_base(metadata=Base.metadata)
LargeText = Text().with_variant(LONGTEXT(), "mysql", "mariadb")


def portable_key_digest(values):
    """为无自然长度上限的精确唯一键生成稳定摘要；NULL 保留数据库唯一键语义。"""
    if any(value is None for value in values):
        return None
    digest = hashlib.sha256()
    for value in values:
        encoded = str(value).encode("utf-8", "surrogatepass")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _digest_default(*columns):
    def default(context):
        parameters = context.get_current_parameters()
        return portable_key_digest(tuple(parameters.get(name) for name in columns))
    return default


def _set_digest(target, columns, digest_column):
    setattr(target, digest_column,
            portable_key_digest(tuple(getattr(target, name) for name in columns)))


class CONFIGFILTERGROUP(Base):
    __tablename__ = 'CONFIG_FILTER_GROUP'

    ID = Column(Integer, primary_key=True)
    GROUP_NAME = Column(Text)
    IS_DEFAULT = Column(Text)
    NOTE = Column(Text)


class CONFIGFILTERRULES(Base):
    __tablename__ = 'CONFIG_FILTER_RULES'

    ID = Column(Integer, primary_key=True)
    GROUP_ID = Column(Text, index=True)
    ROLE_NAME = Column(Text)
    PRIORITY = Column(Text)
    INCLUDE = Column(Text)
    EXCLUDE = Column(Text)
    SIZE_LIMIT = Column(Text)
    NOTE = Column(Text)


class CONFIGRSSPARSER(Base):
    __tablename__ = 'CONFIG_RSS_PARSER'

    ID = Column(Integer, primary_key=True)
    NAME = Column(Text, index=True)
    TYPE = Column(Text)
    FORMAT = Column(Text)
    PARAMS = Column(Text)
    NOTE = Column(Text)
    SYSDEF = Column(Text)


class CONFIGSYNCPATHS(Base):
    __tablename__ = 'CONFIG_SYNC_PATHS'

    ID = Column(Integer, primary_key=True)
    SOURCE = Column(Text)
    DEST = Column(Text)
    UNKNOWN = Column(Text)
    MODE = Column(Text)
    RENAME = Column(Integer)
    ENABLED = Column(Integer)
    NOTE = Column(Text)


class CONFIGUSERS(Base):
    __tablename__ = 'CONFIG_USERS'

    ID = Column(Integer, primary_key=True)
    NAME = Column(Text, index=True)
    PASSWORD = Column(Text)
    PRIS = Column(Text)


class CONFIGUSERRSS(Base):
    __tablename__ = 'CONFIG_USER_RSS'

    ID = Column(Integer, primary_key=True)
    NAME = Column(Text, index=True)
    ADDRESS = Column(Text)
    PARSER = Column(Text)
    INTERVAL = Column(Text)
    USES = Column(Text)
    INCLUDE = Column(Text)
    EXCLUDE = Column(Text)
    FILTER = Column(Text)
    UPDATE_TIME = Column(Text)
    PROCESS_COUNT = Column(Text)
    STATE = Column(Text)
    SAVE_PATH = Column(Text)
    DOWNLOAD_SETTING = Column(Integer)
    RECOGNIZATION = Column(Text)
    OVER_EDITION = Column(Integer)
    SITES = Column(Text)
    FILTER_ARGS = Column(Text)
    MEDIAINFOS = Column(Text)
    NOTE = Column(Text)


class CUSTOMWORDS(Base):
    __tablename__ = 'CUSTOM_WORDS'

    ID = Column(Integer, primary_key=True)
    REPLACED = Column(Text)
    REPLACE = Column(Text)
    FRONT = Column(Text)
    BACK = Column(Text)
    OFFSET = Column(Text)
    TYPE = Column(Integer)
    GROUP_ID = Column(Integer)
    SEASON = Column(Integer)
    ENABLED = Column(Integer)
    REGEX = Column(Integer)
    HELP = Column(Text)
    NOTE = Column(Text)


class CUSTOMWORDGROUPS(Base):
    __tablename__ = 'CUSTOM_WORD_GROUPS'

    ID = Column(Integer, primary_key=True)
    TITLE = Column(Text)
    YEAR = Column(Text)
    TYPE = Column(Integer)
    TMDBID = Column(Integer)
    SEASON_COUNT = Column(Integer)
    NOTE = Column(Text)


class DOUBANMEDIAS(Base):
    __tablename__ = 'DOUBAN_MEDIAS'
    __table_args__ = (
        Index('INDX_DOUBAN_MEDIAS_NAME', 'NAME', 'YEAR'),
    )

    ID = Column(Integer, primary_key=True)
    NAME = Column(Text)
    YEAR = Column(Text)
    TYPE = Column(Text)
    RATING = Column(Text)
    IMAGE = Column(Text)
    STATE = Column(Text)
    ADD_TIME = Column(Text)

    def as_dict(self):
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


class DOWNLOADHISTORY(Base):
    __tablename__ = 'DOWNLOAD_HISTORY'

    ID = Column(Integer, primary_key=True)
    TITLE = Column(Text)
    YEAR = Column(Text)
    TYPE = Column(Text)
    TMDBID = Column(Text)
    VOTE = Column(Text)
    POSTER = Column(Text)
    OVERVIEW = Column(Text)
    TORRENT = Column(Text)
    ENCLOSURE = Column(Text)
    SITE = Column(Text)
    DESC = Column(Text)
    DATE = Column(Text, index=True)

    def as_dict(self):
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


class DOWNLOADSETTING(Base):
    __tablename__ = 'DOWNLOAD_SETTING'

    ID = Column(Integer, primary_key=True)
    NAME = Column(Text)
    CATEGORY = Column(Text)
    TAGS = Column(Text)
    CONTENT_LAYOUT = Column(Integer)
    IS_PAUSED = Column(Integer)
    UPLOAD_LIMIT = Column(Integer)
    DOWNLOAD_LIMIT = Column(Integer)
    RATIO_LIMIT = Column(Integer)
    SEEDING_TIME_LIMIT = Column(Integer)
    DOWNLOADER = Column(Text)
    NOTE = Column(Text)


class MESSAGECLIENT(Base):
    __tablename__ = 'MESSAGE_CLIENT'

    ID = Column(Integer, primary_key=True)
    NAME = Column(Text)
    TYPE = Column(Text)
    CONFIG = Column(Text)
    SWITCHS = Column(Text)
    INTERACTIVE = Column(Integer)
    ENABLED = Column(Integer)
    NOTE = Column(Text)


class RSSHISTORY(Base):
    __tablename__ = 'RSS_HISTORY'

    ID = Column(Integer, primary_key=True)
    TYPE = Column(Text)
    RSSID = Column(Text, index=True)
    NAME = Column(Text)
    YEAR = Column(Text)
    TMDBID = Column(Text)
    SEASON = Column(Text)
    IMAGE = Column(Text)
    DESC = Column(Text)
    TOTAL = Column(Integer)
    START = Column(Integer)
    FINISH_TIME = Column(Text)
    NOTE = Column(Text)

    def as_dict(self):
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


class RSSMOVIES(Base):
    __tablename__ = 'RSS_MOVIES'

    ID = Column(Integer, primary_key=True)
    NAME = Column(Text, index=True)
    YEAR = Column(Text)
    KEYWORD = Column(Text)
    TMDBID = Column(Text)
    IMAGE = Column(Text)
    SEARCH_SITES = Column(Text)
    OVER_EDITION = Column(Integer)
    FILTER_ORDER = Column(Integer)
    FILTER_RESTYPE = Column(Text)
    FILTER_PIX = Column(Text)
    FILTER_RULE = Column(Integer)
    FILTER_TEAM = Column(Text)
    SAVE_PATH = Column(Text)
    DOWNLOAD_SETTING = Column(Integer)
    FUZZY_MATCH = Column(Integer)
    STATE = Column(Text)
    DESC = Column(Text)
    NOTE = Column(Text)

    def as_dict(self):
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


class RSSTORRENTS(Base):
    __tablename__ = 'RSS_TORRENTS'
    __table_args__ = (
        Index('INDX_RSS_TORRENTS_NAME', 'TITLE', 'YEAR', 'SEASON', 'EPISODE'),
        Index('UN_INDX_RSS_TORRENTS_ENCLOSURE', 'ENCLOSURE_HASH', unique=True),
    )
    __portable_unique_keys__ = {("ENCLOSURE",): "ENCLOSURE_HASH"}

    ID = Column(Integer, primary_key=True)
    TORRENT_NAME = Column(Text)
    ENCLOSURE = Column(Text)
    ENCLOSURE_HASH = Column(String(64), nullable=True,
                            default=_digest_default("ENCLOSURE"))
    TYPE = Column(Text)
    TITLE = Column(Text)
    YEAR = Column(Text)
    SEASON = Column(Text)
    EPISODE = Column(Text)


class RSSTVS(Base):
    __tablename__ = 'RSS_TVS'

    ID = Column(Integer, primary_key=True)
    NAME = Column(Text, index=True)
    YEAR = Column(Text)
    KEYWORD = Column(Text)
    SEASON = Column(Text)
    TMDBID = Column(Text)
    IMAGE = Column(Text)
    SEARCH_SITES = Column(Text)
    OVER_EDITION = Column(Integer)
    FILTER_ORDER = Column(Integer)
    FILTER_RESTYPE = Column(Text)
    FILTER_PIX = Column(Text)
    FILTER_RULE = Column(Integer)
    FILTER_TEAM = Column(Text)
    SAVE_PATH = Column(Text)
    DOWNLOAD_SETTING = Column(Integer)
    FUZZY_MATCH = Column(Integer)
    TOTAL_EP = Column(Integer)
    CURRENT_EP = Column(Integer)
    TOTAL = Column(Integer)
    LACK = Column(Integer)
    STATE = Column(Text)
    DESC = Column(Text)
    NOTE = Column(Text)

    def as_dict(self):
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


class RSSTVEPISODES(Base):
    __tablename__ = 'RSS_TV_EPISODES'

    ID = Column(Integer, primary_key=True)
    RSSID = Column(Text, index=True)
    EPISODES = Column(Text)


class TORRENTREMOVETASK(Base):
    __tablename__ = 'TORRENT_REMOVE_TASK'

    ID = Column(Integer, primary_key=True)
    NAME = Column(Text)
    ACTION = Column(Integer)
    INTERVAL = Column(Integer)
    ENABLED = Column(Integer)
    SAMEDATA = Column(Integer)
    ONLYNASTOOL = Column(Integer)
    DOWNLOADER = Column(Text)
    CONFIG = Column(Text)
    NOTE = Column(Text)


class SEARCHRESULTINFO(Base):
    __tablename__ = 'SEARCH_RESULT_INFO'

    ID = Column(Integer, primary_key=True)
    TORRENT_NAME = Column(Text)
    ENCLOSURE = Column(Text)
    DESCRIPTION = Column(Text)
    TYPE = Column(Text)
    TITLE = Column(Text)
    YEAR = Column(Text)
    SEASON = Column(Text)
    EPISODE = Column(Text)
    ES_STRING = Column(Text)
    VOTE = Column(Text)
    IMAGE = Column(Text)
    POSTER = Column(Text)
    TMDBID = Column(Text)
    OVERVIEW = Column(Text)
    RES_TYPE = Column(Text)
    RES_ORDER = Column(Text)
    SIZE = Column(Integer)
    SEEDERS = Column(Integer)
    PEERS = Column(Integer)
    SITE = Column(Text)
    SITE_ORDER = Column(Text)
    PAGEURL = Column(Text)
    OTHERINFO = Column(Text)
    UPLOAD_VOLUME_FACTOR = Column(Float)
    DOWNLOAD_VOLUME_FACTOR = Column(Float)
    NOTE = Column(Text)
    KEYWORD = Column(Text, index=True)


class SEARCHTASK(Base):
    __tablename__ = 'SEARCH_TASK'

    ID = Column(Integer, primary_key=True)
    __table_args__ = (Index("UN_INDX_SEARCH_TASK_KEYWORD_HASH", "KEYWORD_HASH", unique=True),)
    KEYWORD = Column(Text)
    KEYWORD_HASH = Column(String(64), nullable=True,
                          default=_digest_default("KEYWORD"))
    STATUS = Column(Text)
    START_TIME = Column(Text)
    END_TIME = Column(Text)
    MESSAGE = Column(Text)


class AIRECOGNITIONRECORD(Base):
    """需要人工核对的本地解析、AI解析和TMDB匹配记录。"""
    __tablename__ = 'AI_RECOGNITION_RECORD'

    ID = Column(Integer, primary_key=True)
    TITLE = Column(Text, index=True)
    ANITOPY_RESULT = Column(LargeText)
    AI_RESULT = Column(LargeText)
    ANITOPY_TMDB = Column(LargeText)
    AI_TMDB = Column(LargeText)
    STATUS = Column(Text)
    ADD_TIME = Column(Text)

    def as_dict(self):
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


class RECOGNITIONREQUEST(Base):
    """One media-name recognition call and its aggregated raw outcomes."""
    __tablename__ = 'RECOGNITION_REQUEST'

    REQUEST_ID = Column(String(128), primary_key=True)
    ORIGINAL_NAME = Column(Text, index=True)
    SOURCE = Column(Text, index=True)
    STAGE = Column(Text)
    CREATED_AT = Column(Text, index=True)
    SUMMARY = Column(Text)
    CONTEXT = deferred(Column(LargeText))
    ACTIONS = deferred(Column(LargeText))
    PROVIDER_RESULTS = deferred(Column(LargeText))
    OVERALL_RESULT = deferred(Column(LargeText))
    TMDB_RESULTS = deferred(Column(LargeText))


class RECOGNITIONATTEMPT(Base):
    """A discoverable provider attempt attached to a recognition request."""
    __tablename__ = 'RECOGNITION_ATTEMPT'

    ID = Column(Integer, primary_key=True)
    REQUEST_ID = Column(Text, index=True)
    ATTEMPT_ID = Column(Text, index=True)
    PROVIDER_ID = Column(Text, index=True)
    STATUS = Column(Text, index=True)
    INPUT = Column(LargeText)
    RAW_RESULT = Column(LargeText)
    NORMALIZED_RESULT = Column(LargeText)
    TMDB_RESULTS = Column(LargeText)
    ERROR = Column(LargeText)
    ELAPSED_MS = Column(Integer)


class SYNCHISTORY(Base):
    __tablename__ = 'SYNC_HISTORY'
    __table_args__ = (
        Index('UN_INDX_SYNC_HISTORY_PATH_DEST', 'PATH_DEST_HASH', unique=True),
    )
    __portable_unique_keys__ = {("PATH", "DEST"): "PATH_DEST_HASH"}

    ID = Column(Integer, primary_key=True)
    PATH = Column(Text, index=True)
    SRC = Column(Text)
    DEST = Column(Text)
    PATH_DEST_HASH = Column(String(64), nullable=True,
                            default=_digest_default("PATH", "DEST"))


class SYSTEMDICT(Base):
    __tablename__ = 'SYSTEM_DICT'
    __table_args__ = (
        Index('INDX_SYSTEM_DICT', 'TYPE', 'KEY'),
    )

    ID = Column(Integer, primary_key=True)
    TYPE = Column(Text)
    KEY = Column(Text)
    VALUE = Column(Text)
    NOTE = Column(Text)


class TRANSFERBLACKLIST(Base):
    __tablename__ = 'TRANSFER_BLACKLIST'

    ID = Column(Integer, primary_key=True)
    PATH = Column(Text, index=True)


class TRANSFERHISTORY(Base):
    __tablename__ = 'TRANSFER_HISTORY'
    __table_args__ = (
        Index('INDX_TRANSFER_HISTORY_SOURCE_DEST',
              'SOURCE_PATH', 'SOURCE_FILENAME', 'DEST_PATH', 'DEST_FILENAME'),
    )

    ID = Column(Integer, primary_key=True)
    MODE = Column(Text)
    TYPE = Column(Text)
    CATEGORY = Column(Text)
    TMDBID = Column(Integer)
    TITLE = Column(Text, index=True)
    YEAR = Column(Text)
    SEASON_EPISODE = Column(Text)
    SOURCE = Column(Text)
    SOURCE_PATH = Column(Text, index=True)
    SOURCE_FILENAME = Column(Text, index=True)
    DEST = Column(Text)
    DEST_PATH = Column(Text)
    DEST_FILENAME = Column(Text)
    DATE = Column(Text)

    def as_dict(self):
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


class TRANSFERUNKNOWN(Base):
    __tablename__ = 'TRANSFER_UNKNOWN'

    ID = Column(Integer, primary_key=True)
    PATH = Column(Text, index=True)
    DEST = Column(Text)
    MODE = Column(Text)
    STATE = Column(Text, index=True)


class USERRSSTASKHISTORY(Base):
    __tablename__ = 'USERRSS_TASK_HISTORY'

    ID = Column(Integer, primary_key=True)
    TASK_ID = Column(Text, index=True)
    TITLE = Column(Text)
    DOWNLOADER = Column(Text)
    DATE = Column(Text)


class MEDIASYNCITEMS(BaseMedia):
    __tablename__ = 'MEDIASYNC_ITEMS'
    __table_args__ = (
        Index('INDX_MEDIASYNC_ITEMS_SL', 'SERVER', 'LIBRARY'),
        Index('UN_INDX_MEDIASYNC_ITEMS_SERVER_GENERATION_ITEM',
              'SERVER', 'GENERATION_ID', 'ITEM_ID', unique=True),
    )

    ID = Column(Integer, primary_key=True)
    SERVER = Column(String(128))
    GENERATION_ID = Column(String(64), nullable=False, default=MEDIA_SYNC_LEGACY_GENERATION,
                           server_default=MEDIA_SYNC_LEGACY_GENERATION)
    LIBRARY = Column(Text)
    ITEM_ID = Column(String(256), index=True)
    ITEM_TYPE = Column(Text)
    TITLE = Column(Text, index=True)
    ORGIN_TITLE = Column(Text, index=True)
    YEAR = Column(Text)
    TMDBID = Column(Text, index=True)
    IMDBID = Column(Text)
    PATH = Column(Text)
    NOTE = Column(Text)
    JSON = Column(LargeText)


@event.listens_for(RSSTORRENTS, "before_insert")
@event.listens_for(RSSTORRENTS, "before_update")
def _sync_torrent_enclosure_hash(mapper, connection, target):
    _set_digest(target, ("ENCLOSURE",), "ENCLOSURE_HASH")


@event.listens_for(SEARCHTASK, "before_insert")
@event.listens_for(SEARCHTASK, "before_update")
def _sync_search_task_hash(mapper, connection, target):
    _set_digest(target, ("KEYWORD",), "KEYWORD_HASH")


@event.listens_for(SYNCHISTORY, "before_insert")
@event.listens_for(SYNCHISTORY, "before_update")
def _sync_sync_history_hash(mapper, connection, target):
    _set_digest(target, ("PATH", "DEST"), "PATH_DEST_HASH")


class MEDIASYNCSTATISTIC(BaseMedia):
    __tablename__ = 'MEDIASYNC_STATISTICS'
    __table_args__ = (Index('UN_INDX_MEDIASYNC_STATISTICS_SERVER', 'SERVER', unique=True),)

    ID = Column(Integer, primary_key=True)
    SERVER = Column(Text)
    ACTIVE_GENERATION = Column(String(64), nullable=False, default=MEDIA_SYNC_LEGACY_GENERATION,
                               server_default=MEDIA_SYNC_LEGACY_GENERATION)
    TOTAL_COUNT = Column(Text)
    MOVIE_COUNT = Column(Text)
    TV_COUNT = Column(Text)
    UPDATE_TIME = Column(Text)


class MEDIASYNCSYNCJOB(BaseMedia):
    """每个媒体服务器至多一个有效的全量同步租约。"""
    __tablename__ = "MEDIASYNC_SYNC_JOBS"
    __table_args__ = (Index("UN_INDX_MEDIASYNC_SYNC_JOBS_JOB_ID", "JOB_ID", unique=True),)

    SERVER = Column(String(128), primary_key=True)
    JOB_ID = Column(String(64), nullable=False)
    GENERATION_ID = Column(String(64), nullable=False)
    LEASE_TOKEN = Column(BigInteger, nullable=False)
    LEASE_UNTIL = Column(Float, nullable=False)
    EXPECTED_ACTIVE_GENERATION = Column(String(64))
    STATUS = Column(String(32), nullable=False)
    CREATED_AT = Column(Float, nullable=False)
    UPDATED_AT = Column(Float, nullable=False)


class DATABASEMIGRATIONMARKER(Base):
    """保存已验证数据库切换代次，供新进程在开放业务前确认目标。"""
    __tablename__ = "DATABASE_MIGRATION_MARKERS"

    ID = Column(String(64), primary_key=True)
    STATUS = Column(String(32), nullable=False)
    SOURCE_FINGERPRINT = Column(String(64), nullable=False)
    TARGET_FINGERPRINT = Column(String(64), nullable=False)
    COMPLETED_AT = Column(Text, nullable=False)


# 非唯一全文索引在 MySQL/MariaDB 使用固定前缀，在 PostgreSQL 上不创建可能
# 因超长路径或标题而失败的 btree 索引；SQLite 继续保留完整索引。
for _table in Base.metadata.tables.values():
    for _index in _table.indexes:
        _text_columns = [column for column in _index.columns
                         if isinstance(column.type, Text)]
        if _text_columns and not _index.unique:
            _index.dialect_options["mysql"]["length"] = {
                column.name: 191 for column in _text_columns
            }
            _index.ddl_if(dialect=("sqlite", "mysql", "mariadb"))
