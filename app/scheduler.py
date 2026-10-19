import datetime
import traceback

from apscheduler.executors.pool import ThreadPoolExecutor
import log
from app.db.scheduler import DatabaseSessionBackgroundScheduler
from app.db.main_db import MainDb
from app.db.media_db import MediaDb
from app.doubansync import DoubanSync
from app.downloader import Downloader
from app.helper import DbHelper, MetaHelper
from app.helper.db_helper import DEFAULT_HISTORY_LIMITS, history_record_limit
from app.mediaserver import MediaServer
from app.subscribe import Subscribe
from app.sync import Sync
from app.utils import ExceptionUtils
from app.utils.commons import singleton
from config import PT_TRANSFER_INTERVAL, METAINFO_SAVE_INTERVAL, \
    SYNC_TRANSFER_INTERVAL, RSS_CHECK_INTERVAL, \
    RSS_REFRESH_TMDB_INTERVAL, META_DELETE_UNKNOWN_INTERVAL, REFRESH_WALLPAPER_INTERVAL, Config
from web.backend.wallpaper import get_login_wallpaper


def _cleanup_options(config, default_retention=30, legacy_retention_key=None, default_max_records=200):
    """读取并检查历史清理周期、保留时间与记录上限。"""
    config = config or {}
    try:
        interval_hours = int(config.get('interval_hours', 24))
        retention_value = config.get('retention_value')
        if retention_value is None and legacy_retention_key:
            retention_value = config.get(legacy_retention_key)
        retention_value = int(retention_value if retention_value is not None else default_retention)
        max_records = history_record_limit(config.get('max_records', default_max_records))
    except (TypeError, ValueError):
        return None
    retention_unit = config.get('retention_unit', 'day')
    max_retention = 36500 if retention_unit == 'day' else 1200 if retention_unit == 'month' else 0
    if not 1 <= interval_hours <= 8760 or not 1 <= retention_value <= max_retention:
        return None
    return interval_hours, retention_value, retention_unit, max_records


def _retention_label(value, unit):
    return f"{value} {'天' if unit == 'day' else '个月'}"


@singleton
class Scheduler:
    SCHEDULER = None
    _pt = None
    _douban = None
    _media = None
    _recognition = None
    _history = None

    def __init__(self):
        self.init_config()

    def init_config(self):
        self._pt = Config().get_config('pt')
        self._media = Config().get_config('media')
        self._douban = Config().get_config('douban')
        self._recognition = Config().get_config('recognition')
        self._history = Config().get_config('history')

    def run_service(self):
        """
        读取配置，启动定时服务
        """
        # Scheduler 是单例；配置更新后重启时需重新读取，避免沿用旧的识别清理开关。
        self.init_config()
        self.SCHEDULER = DatabaseSessionBackgroundScheduler(timezone=Config().get_timezone(),
                                             executors={
                                                 'default': ThreadPoolExecutor(20)
                                             },
                                             job_defaults={
                                                 'max_instances': 1,
                                                 'coalesce': True,
                                                 'misfire_grace_time': 300
                                             })
        if not self.SCHEDULER:
            return
        if self._pt:
            # 下载文件转移
            pt_monitor = self._pt.get('pt_monitor')
            if pt_monitor:
                self.SCHEDULER.add_job(Downloader().transfer, 'interval', seconds=PT_TRANSFER_INTERVAL)
                log.info("下载文件转移服务启动")

            # RSS订阅定时检索
            search_rss_interval = self._pt.get('search_rss_interval')
            if search_rss_interval:
                if isinstance(search_rss_interval, str) and search_rss_interval.isdigit():
                    search_rss_interval = int(search_rss_interval)
                else:
                    try:
                        search_rss_interval = round(float(search_rss_interval))
                    except Exception as e:
                        log.error("订阅定时搜索周期 配置格式错误：%s" % str(e))
                        search_rss_interval = 0
                if search_rss_interval:
                    if search_rss_interval < 6:
                        search_rss_interval = 6
                    self.SCHEDULER.add_job(Subscribe().subscribe_search_all, 'interval', hours=search_rss_interval)
                    log.info("订阅定时搜索服务启动")

        # 豆瓣电影同步
        if self._douban:
            douban_interval = self._douban.get('interval')
            if douban_interval:
                if isinstance(douban_interval, str):
                    if douban_interval.isdigit():
                        douban_interval = int(douban_interval)
                    else:
                        try:
                            douban_interval = float(douban_interval)
                        except Exception as e:
                            log.info("豆瓣同步服务启动失败：%s" % str(e))
                            douban_interval = 0
                if douban_interval:
                    self.SCHEDULER.add_job(DoubanSync().sync, 'interval', hours=douban_interval)
                    log.info("豆瓣同步服务启动")

        # 媒体库同步
        if self._media:
            mediasync_interval = self._media.get("mediasync_interval")
            if mediasync_interval:
                if isinstance(mediasync_interval, str):
                    if mediasync_interval.isdigit():
                        mediasync_interval = int(mediasync_interval)
                    else:
                        try:
                            mediasync_interval = round(float(mediasync_interval))
                        except Exception as e:
                            log.info("豆瓣同步服务启动失败：%s" % str(e))
                            mediasync_interval = 0
                if mediasync_interval:
                    self.SCHEDULER.add_job(MediaServer().sync_mediaserver, 'interval', hours=mediasync_interval)
                    log.info("媒体库同步服务启动")

        # 元数据定时保存
        self.SCHEDULER.add_job(MetaHelper().save_meta_data, 'interval', seconds=METAINFO_SAVE_INTERVAL)

        # 定时把队列中的监控文件转移走
        self.SCHEDULER.add_job(Sync().transfer_mon_files, 'interval', seconds=SYNC_TRANSFER_INTERVAL)

        # RSS队列中检索
        self.SCHEDULER.add_job(Subscribe().subscribe_search, 'interval', seconds=RSS_CHECK_INTERVAL)

        # 豆瓣RSS转TMDB，定时更新TMDB数据
        self.SCHEDULER.add_job(Subscribe().refresh_rss_metainfo, 'interval', hours=RSS_REFRESH_TMDB_INTERVAL)

        # 定时清除未识别的缓存
        self.SCHEDULER.add_job(MetaHelper().delete_unknown_meta, 'interval', hours=META_DELETE_UNKNOWN_INTERVAL)

        # 定时刷新壁纸
        self.SCHEDULER.add_job(get_login_wallpaper,
                               'interval',
                               hours=REFRESH_WALLPAPER_INTERVAL,
                               next_run_time=datetime.datetime.now())

        # 定时 WAL checkpoint，防止 WAL 文件过度膨胀
        self.SCHEDULER.add_job(MainDb.wal_checkpoint, 'interval', hours=6)

        # 定时清理超过保留期限的媒体识别记录。
        record_cleanup = (((self._recognition or {}).get('records') or {}).get('cleanup') or {})
        if record_cleanup.get('enabled'):
            cleanup_options = _cleanup_options(
                record_cleanup, default_retention=30, legacy_retention_key='retention_days')
            if cleanup_options:
                interval_hours, retention_value, retention_unit, max_records = cleanup_options
                self.SCHEDULER.add_job(
                    self.cleanup_expired_recognition_records,
                    'interval', hours=interval_hours,
                    id='recognition_record_cleanup', replace_existing=True,
                    next_run_time=datetime.datetime.now())
                log.info("媒体识别记录过期清理服务启动，每 %s 小时执行，保留 %s，记录上限 %s 条" % (
                    interval_hours, _retention_label(retention_value, retention_unit), max_records))

        history_config = (self._history or {}).get('cleanup') or {}
        history_jobs = (
            ('transfer_history', 'transfer_history_cleanup',
             self.cleanup_expired_transfer_history, '转移历史'),
            ('subscription_history', 'subscription_history_cleanup',
             self.cleanup_expired_subscription_history, '订阅历史'),
            ('download_history', 'download_history_cleanup',
             self.cleanup_expired_download_history, '最近下载'),
        )
        for config_key, job_id, cleanup_func, label in history_jobs:
            cleanup_config = history_config.get(config_key) or {}
            if not cleanup_config.get('enabled'):
                continue
            cleanup_options = _cleanup_options(cleanup_config, default_max_records=DEFAULT_HISTORY_LIMITS[config_key])
            if not cleanup_options:
                log.error("%s过期清理配置无效，任务未启动" % label)
                continue
            interval_hours, retention_value, retention_unit, max_records = cleanup_options
            self.SCHEDULER.add_job(
                cleanup_func, 'interval', hours=interval_hours,
                id=job_id, replace_existing=True,
                next_run_time=datetime.datetime.now())
            log.info("%s过期清理服务启动，每 %s 小时执行，保留 %s，记录上限 %s 条" % (
                label, interval_hours, _retention_label(retention_value, retention_unit), max_records))

        self.SCHEDULER.print_jobs()

        self.SCHEDULER.start()

    def stop_service(self):
        """
        停止定时服务
        """
        try:
            if self.SCHEDULER:
                self.SCHEDULER.remove_all_jobs()
                self.SCHEDULER.shutdown()
                self.SCHEDULER = None
        except Exception as e:
            ExceptionUtils.exception_traceback(e)

    @staticmethod
    def cleanup_expired_recognition_records():
        """按当前保留期限和条数上限清理识别记录及其解析器明细。"""
        recognition = Config().get_config('recognition') or {}
        cleanup = ((recognition.get('records') or {}).get('cleanup') or {})
        options = _cleanup_options(cleanup, default_retention=30,
                                   legacy_retention_key='retention_days')
        if not options:
            log.error("媒体识别记录过期清理配置无效")
            return 0
        _, retention_value, retention_unit, max_records = options
        deleted_count = DbHelper().delete_expired_recognition_records(
            retention_unit=retention_unit, retention_value=retention_value, max_records=max_records)
        if deleted_count is False:
            raise RuntimeError("媒体识别记录清理失败，请查看数据库错误日志")
        return deleted_count

    @staticmethod
    def _cleanup_expired_history(config_key, delete_method, label):
        history = Config().get_config('history') or {}
        cleanup = ((history.get('cleanup') or {}).get(config_key) or {})
        options = _cleanup_options(cleanup, default_max_records=DEFAULT_HISTORY_LIMITS[config_key])
        if not options:
            log.error("%s过期清理配置无效" % label)
            return 0
        _, retention_value, retention_unit, max_records = options
        deleted_count = delete_method(retention_value, retention_unit=retention_unit, max_records=max_records)
        if deleted_count is False:
            raise RuntimeError("%s过期清理失败，请查看数据库错误日志" % label)
        return deleted_count

    def cleanup_expired_transfer_history(self):
        return self._cleanup_expired_history(
            'transfer_history', DbHelper().delete_expired_transfer_history, '转移历史')

    def cleanup_expired_subscription_history(self):
        return self._cleanup_expired_history(
            'subscription_history', DbHelper().delete_expired_subscription_history, '订阅历史')

    def cleanup_expired_download_history(self):
        return self._cleanup_expired_history(
            'download_history', DbHelper().delete_expired_download_history, '最近下载')


def run_scheduler():
    """
    启动定时服务
    """
    try:
        Scheduler().run_service()
    except Exception as err:
        log.error("启动定时服务失败：%s - %s" % (str(err), traceback.format_exc()))


def stop_scheduler():
    """
    停止定时服务
    """
    try:
        Scheduler().stop_service()
    except Exception as err:
        log.debug("停止定时服务失败：%s" % str(err))


def restart_scheduler():
    """
    重启定时服务
    """
    stop_scheduler()
    run_scheduler()
