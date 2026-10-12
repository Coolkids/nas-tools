import os
import signal
import sys
import time
import warnings

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
warnings.filterwarnings('ignore')

# Windows 打包版使用可执行文件旁的配置目录；托盘界面已移除。
is_windows_exe = getattr(sys, 'frozen', False) and os.name == "nt"
if is_windows_exe:
    config_dir = os.path.join(os.path.dirname(sys.executable), "config").replace("\\", "/")
    os.environ["NASTOOL_CONFIG"] = os.path.join(config_dir, "config.yaml")
    os.environ["NASTOOL_LOG"] = os.path.join(config_dir, "logs")
    os.makedirs(config_dir, exist_ok=True)

from config import Config
import log
from web.main import App
from app.utils import SystemUtils, ConfigLoadCache, Torrent
from app.utils.commons import INSTANCES
from app.db import init_db, update_db, init_data
from app.helper import DisplayHelper, ChromeHelper
from app.rsschecker import RssChecker
from app.scheduler import run_scheduler, restart_scheduler
from app.sync import run_monitor, restart_monitor
from app.torrentremover import TorrentRemover
from app.speedlimiter import SpeedLimiter
from check_config import update_config, check_config
from version import APP_VERSION


def sigal_handler(num, stack):
    """
    信号处理
    """
    log.warn('捕捉到退出信号：%s，开始退出...' % num)
    if SystemUtils.is_docker():
        # 停止虚拟显示
        DisplayHelper().quit()
    # 退出主进程；直接运行和容器运行都必须尊重停止/重启信号。
    sys.exit(0)


def get_run_config():
    """
    获取运行配置
    """
    _web_host = "::"
    _web_port = 3000
    _ssl_cert = None
    _ssl_key = None
    _debug = False

    app_conf = Config().get_config('app')
    if app_conf:
        if app_conf.get("web_host"):
            _web_host = app_conf.get("web_host").replace('[', '').replace(']', '')
        _web_port = int(app_conf.get('web_port')) if str(app_conf.get('web_port', '')).isdigit() else 3000
        _ssl_cert = app_conf.get('ssl_cert')
        _ssl_key = app_conf.get('ssl_key')
        _ssl_key = app_conf.get('ssl_key')
        _debug = True if app_conf.get("debug") else False

    app_arg = dict(host=_web_host, port=_web_port, debug=_debug, threaded=True, use_reloader=False)
    if _ssl_cert:
        app_arg['ssl_context'] = (_ssl_cert, _ssl_key)
    return app_arg


# 退出事件
signal.signal(signal.SIGINT, sigal_handler)
signal.signal(signal.SIGTERM, sigal_handler)


def init_system():
    # 配置
    log.console('NAStool 当前版本号：%s' % APP_VERSION)
    # 旧双库合并必须早于 ORM 建表，避免在旧数据旁误建空单库。
    try:
        from app.utils.storage_jobs import STORAGE_JOBS
        from app.db.sqlite_files import prepare_sqlite_database
        # 启动结构升级使用跨进程独占锁，避免两个新版本进程同时迁移同一目录。
        with STORAGE_JOBS.maintenance_lock(exclusive=True):
            from app.db.migration import verify_database_startup
            verify_database_startup()
            prepare_sqlite_database()
            update_db()
            init_db()
            init_data()
            # 配置升级会读取数据库，也保持在启动结构锁内，避免并发初始化竞争。
            update_config()
            check_config()
    except Exception as e:
        log.error(f"数据库布局检查或合库失败：{e}")
        raise
    try:
        from app.db.sqlite_files import finalize_sqlite_migration
        finalize_sqlite_migration()
    except Exception as e:
        # 新单库已通过结构检查；归档失败留待下次启动重试。
        log.error(f"旧数据库归档待重试：{e}")
    # 清理上次运行遗留在配置 temp 目录中的种子文件
    Torrent().clean_temp_torrent_files()
    # 数据库结构就绪后回放未完成的全量识别记录暂存。
    try:
        from app.media.recognition.records import replay_recognition_spool
        replay_recognition_spool()
    except Exception as e:
        log.error(f"恢复识别记录暂存失败：{e}")
    # 本地缓存布局升级与旧数据导入必须在定时任务和后台服务启动前完成。
    try:
        from app.utils.cache_backend import STORE
        STORE.initialize()
        from app.utils.cache_migration import verify_cache_startup
        verify_cache_startup()
    except Exception as e:
        log.error(f"本地缓存存储初始化失败：{e}")
        raise
    # 恢复异常中断的搜索任务
    try:
        from web.backend.search_task import SearchTaskPool
        SearchTaskPool.recover_crashed_tasks()
    except Exception as e:
        log.error(f"恢复搜索任务失败：{e}")


def start_service():
    log.console("开始启动服务...")
    # 启动虚拟显示
    DisplayHelper()
    # 启动定时服务
    run_scheduler()
    # 启动监控服务
    run_monitor()
    # 启动自定义订阅服务
    RssChecker()
    # 启动自动删种服务
    TorrentRemover()
    # 启动播放限速服务
    SpeedLimiter()
    # 初始化浏览器
    if not is_windows_exe:
        ChromeHelper().init_driver()


def monitor_config():
    class _ConfigHandler(FileSystemEventHandler):
        """
        配置文件变化响应
        """

        def __init__(self):
            FileSystemEventHandler.__init__(self)

        @staticmethod
        def _reload_config(path):
            if os.path.basename(path) != "config.yaml":
                return
            from app.utils.storage_jobs import STORAGE_JOBS
            if STORAGE_JOBS.maintenance_gates():
                log.console("存储切换任务尚未完成，暂不热加载配置文件")
                return
            # 10秒内只能加载一次；原子替换会产生 moved/created 等不同事件。
            if ConfigLoadCache.get(path):
                return
            ConfigLoadCache.set(path, True)
            log.console("进程 %s 检测到配置文件已修改，正在重新加载..." % os.getpid())
            time.sleep(1)
            Config().init_config()
            for instance in INSTANCES.values():
                if hasattr(instance, "init_config"):
                    instance.init_config()
            restart_scheduler()
            restart_monitor()

        def on_modified(self, event):
            if not event.is_directory:
                self._reload_config(event.src_path)

        def on_created(self, event):
            if not event.is_directory:
                self._reload_config(event.src_path)

        def on_moved(self, event):
            if not event.is_directory:
                self._reload_config(event.dest_path)

    # 配置文件监听
    _observer = Observer(timeout=10)
    _observer.schedule(_ConfigHandler(), path=Config().get_config_path(), recursive=False)
    _observer.daemon = True
    _observer.start()


# 系统初始化
init_system()

# 启动服务
start_service()

# 监听配置文件变化
monitor_config()

# 本地运行
if __name__ == '__main__':
    # gunicorn 启动
    App.run(**get_run_config())
