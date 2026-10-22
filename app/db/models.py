# coding: utf-8
from collections.abc import Mapping
import hashlib
import json

from sqlalchemy import BigInteger, Column, Float, ForeignKey, Index, Integer, LargeBinary, String, Table, Text, event, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import deferred
from sqlalchemy.dialects.mysql import LONGBLOB, LONGTEXT

MEDIA_SYNC_LEGACY_GENERATION = "legacy"

Base = declarative_base()
# 两个业务逻辑域共用一份 metadata 和结构版本。
BaseMedia = declarative_base(metadata=Base.metadata)
LargeText = Text().with_variant(LONGTEXT(), "mysql", "mariadb")

# 每个调度器独立读取自己的 JobStore，避免多个调度器重复执行同一任务。
SCHEDULER_JOB_TABLES = {
    namespace: Table(
        name, Base.metadata,
        Column("id", String(191), primary_key=True),
        Column("next_run_time", Float(precision=53), index=True),
        Column("job_state", LargeBinary().with_variant(LONGBLOB(), "mysql", "mariadb"), nullable=False),
    ) for namespace, name in (
        ("main", "SCHEDULER_JOBS"),
        ("rss", "RSS_SCHEDULER_JOBS"),
        ("torrent_remover", "TORRENT_REMOVER_JOBS"),
        ("speed_limiter", "SPEED_LIMITER_JOBS"),
    )
}


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


def validate_string_lengths(table, rows):
    """在 SQLite 等不强制执行 VARCHAR 长度的后端拒绝超长值。"""
    table = getattr(table, "__table__", table)
    bounded_columns = [column for column in table.columns
                       if isinstance(column.type, String) and column.type.length]
    for row in rows:
        for column in bounded_columns:
            value = row.get(column.name) if isinstance(row, Mapping) else getattr(
                row, column.name, None)
            if value is not None and len(str(value)) > column.type.length:
                raise ValueError(
                    f"{table.name}.{column.name} 超过 {column.type.length} 个字符，拒绝截断")


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
    TMDBID = Column(String(64))
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
    RSSID = Column(String(128), index=True)
    NAME = Column(Text)
    YEAR = Column(Text)
    TMDBID = Column(String(64))
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
    TMDBID = Column(String(64))
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
    TMDBID = Column(String(64))
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
    RSSID = Column(String(128), index=True)
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
    TMDBID = Column(String(64))
    OVERVIEW = Column(Text)
    RES_TYPE = Column(Text)
    RES_ORDER = Column(Text)
    SIZE = Column(Text)
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
    __portable_unique_keys__ = {("KEYWORD",): "KEYWORD_HASH"}

    ID = Column(Integer, primary_key=True)
    __table_args__ = (Index("UN_INDX_SEARCH_TASK_KEYWORD_HASH", "KEYWORD_HASH", unique=True),)
    KEYWORD = Column(Text)
    KEYWORD_HASH = Column(String(64), nullable=True,
                          default=_digest_default("KEYWORD"))
    STATUS = Column(Text)
    START_TIME = Column(Text)
    END_TIME = Column(Text)
    MESSAGE = Column(Text)


class RECOGNITIONTRACE(Base):
    __tablename__ = "RECOGNITION_TRACE"
    REQUEST_ID = Column(String(128), primary_key=True)
    STARTED_AT = Column(String(32), nullable=False, index=True)
    FINISHED_AT = Column(String(32), index=True)
    STARTED_MONOTONIC_NS = Column(BigInteger, nullable=False)
    STATUS = Column(String(32), nullable=False, index=True)
    RECORD_MODE = Column(String(16), nullable=False)
    SOURCE = Column(String(128), nullable=False)
    VERSION = Column(Integer, nullable=False, default=0)
    INPUT = deferred(Column(LargeText))
    OUTCOME = deferred(Column(LargeText))
    EXPORT_UNTIL = Column(Float(precision=53), nullable=False, default=0)
    __table_args__ = (Index("IDX_V3_TRACE_FINISH_ID", "FINISHED_AT", "REQUEST_ID"),)


class RECOGNITIONEVENT(Base):
    __tablename__ = "RECOGNITION_EVENT"
    REQUEST_ID = Column(String(128), ForeignKey("RECOGNITION_TRACE.REQUEST_ID", ondelete="CASCADE"), primary_key=True)
    SEQ = Column(Integer, primary_key=True)
    NODE_ID = Column(String(191), nullable=False)
    STATUS = Column(String(32), nullable=False)
    PAYLOAD = deferred(Column(LargeText, nullable=False))


class RECOGNITIONCANDIDATE(Base):
    __tablename__ = "RECOGNITION_CANDIDATE"
    REQUEST_ID = Column(String(128), ForeignKey("RECOGNITION_TRACE.REQUEST_ID", ondelete="CASCADE"), primary_key=True)
    MEDIA_TYPE = Column(String(16), primary_key=True)
    TMDB_ID = Column(Integer, primary_key=True)
    RANK = Column(Integer)
    PAYLOAD = deferred(Column(LargeText, nullable=False))


class RECOGNITIONTASK(Base):
    __tablename__ = "RECOGNITION_TASK"
    TASK_ID = Column(String(128), primary_key=True)
    REQUEST_ID = Column(String(128), index=True)
    OWNER_ID = Column(String(128), nullable=False)
    KIND = Column(String(32), nullable=False)
    STATE = Column(String(32), nullable=False, index=True)
    LEASE_UNTIL = Column(Float(precision=53), nullable=False, default=0)
    CREATED_AT = Column(String(32), nullable=False)
    CANCEL_REASON = Column(String(64))
    CONTROL = Column(LargeText)
    # CONTROL is strictly allowlisted operational metadata, never input/result.


class RECOGNITIONOVERRIDE(Base):
    __tablename__ = "RECOGNITION_OVERRIDE"
    OVERRIDE_KEY = Column(String(64), primary_key=True)
    VERSION = Column(Integer, nullable=False)
    PAYLOAD = Column(LargeText, nullable=False)


class RECOGNITIONSECRET(Base):
    __tablename__ = "RECOGNITION_SECRET"
    NAME = Column(String(128), primary_key=True)
    KEY_ID = Column(String(128), nullable=False)
    CIPHERTEXT = Column(LargeBinary().with_variant(LONGBLOB(), "mysql", "mariadb"), nullable=False)


class RECOGNITIONRECORDSTATE(Base):
    __tablename__ = "RECOGNITION_RECORD_STATE"
    STATE_ID = Column(String(64), primary_key=True)
    VERSION = Column(Integer, nullable=False, default=0)
    CLEAR_FINISHED_BEFORE = Column(String(32))
    CLEANUP_OWNER = Column(String(128))
    CLEANUP_UNTIL = Column(Float(precision=53), nullable=False, default=0)
    STATE = Column(LargeText)


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
    TASK_ID = Column(String(128), index=True)
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
    TMDBID = Column(String(64), index=True)
    IMDBID = Column(String(32))
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
    SERVER = Column(String(128))
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
    # A MySQL FLOAT only retains ~7 decimal digits; Unix epoch seconds are
    # therefore rounded by minutes and can make an active lease appear stale.
    # Precision 53 selects DOUBLE on MySQL/MariaDB and double precision on
    # PostgreSQL while preserving SQLite's normal REAL affinity.
    LEASE_UNTIL = Column(Float(precision=53), nullable=False)
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
def configure_portable_indexes(metadata):
    # Table.to_metadata does not retain Index dialect options / DDL conditions.
    # Archived portable restore schemas need the same policy as runtime tables.
    for table in metadata.tables.values():
        for index in table.indexes:
            text_columns = [column for column in index.columns if isinstance(column.type, Text)]
            if text_columns and not index.unique:
                text_prefixes = {column.name: 191 for column in text_columns}
                index.dialect_options["mysql"]["length"] = text_prefixes
                index.dialect_options["mariadb"]["length"] = text_prefixes
                index.ddl_if(dialect=("sqlite", "mysql", "mariadb"))


configure_portable_indexes(Base.metadata)
