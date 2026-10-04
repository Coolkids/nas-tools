import datetime
import traceback

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler

import log
from app.db.main_db import MainDb
from app.db.media_db import MediaDb
from app.doubansync import DoubanSync
from app.downloader import Downloader
from app.helper import DbHelper, MetaHelper
from app.mediaserver import MediaServer
from app.subscribe import Subscribe
from app.sync import Sync
from app.utils import ExceptionUtils
from app.utils.commons import singleton
from config import PT_TRANSFER_INTERVAL, METAINFO_SAVE_INTERVAL, \
    SYNC_TRANSFER_INTERVAL, RSS_CHECK_INTERVAL, \
    RSS_REFRESH_TMDB_INTERVAL, META_DELETE_UNKNOWN_INTERVAL, REFRESH_WALLPAPER_INTERVAL, Config
from web.backend.wallpaper import get_login_wallpaper


@singleton
class Scheduler:
    SCHEDULER = None
    _pt = None
    _douban = None
    _media = None
    _recognition = None

    def __init__(self):
        self.init_config()

    def init_config(self):
        self._pt = Config().get_config('pt')
        self._media = Config().get_config('media')
        self._douban = Config().get_config('douban')
        self._recognition = Config().get_config('recognition')

    def run_service(self):
        """
        读取配置，启动定时服务
        """
        self.SCHEDULER = BackgroundScheduler(timezone=Config().get_timezone(),
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
        self.SCHEDULER.add_job(MediaDb.wal_checkpoint, 'interval', hours=6)

        # 定时清理超过保留期限的媒体识别记录。
        record_cleanup = (((self._recognition or {}).get('records') or {}).get('cleanup') or {})
        if record_cleanup.get('enabled'):
            try:
                retention_days = int(record_cleanup.get('retention_days', 30))
            except (TypeError, ValueError):
                retention_days = 30
            if 1 <= retention_days <= 36500:
                self.SCHEDULER.add_job(
                    self.cleanup_expired_recognition_records,
                    'interval', days=1,
                    id='recognition_record_cleanup', replace_existing=True)
                log.info("媒体识别记录过期清理服务启动，保留 %s 天" % retention_days)

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
        """按当前配置删除过期识别记录及其解析器明细。"""
        recognition = Config().get_config('recognition') or {}
        cleanup = ((recognition.get('records') or {}).get('cleanup') or {})
        try:
            retention_days = int(cleanup.get('retention_days', 30))
        except (TypeError, ValueError):
            retention_days = 30
        deleted_count = DbHelper().delete_expired_recognition_records(retention_days)
        log.info("媒体识别记录过期清理完成，保留 %s 天，删除 %s 条" % (
            retention_days, deleted_count))
        return deleted_count

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
