"""APScheduler integration that releases thread-scoped database sessions."""

from functools import wraps
from importlib import import_module
from contextlib import contextmanager
from datetime import datetime, timezone
import pickle

from apscheduler.jobstores.base import BaseJobStore
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.base import STATE_RUNNING, STATE_STOPPED
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.util import undefined
from sqlalchemy import select

from app.db.manager import DATABASE
from app.db.models import SCHEDULER_JOB_TABLES
from app.utils.storage_jobs import STORAGE_JOBS


@contextmanager
def _jobstore_access():
    # JobStore 使用 Core SQL，也需遵守数据库备份、迁移和恢复的维护锁。
    with STORAGE_JOBS.maintenance_lock(exclusive=False, timeout=2):
        if STORAGE_JOBS.maintenance_gate("database"):
            raise RuntimeError("数据库正在维护，暂不读写定时任务")
        yield


def get_persistent_job_schedules():
    """只读持久化执行时间，不反序列化任务正文或初始化业务调度器。"""
    schedules = []
    with _jobstore_access(), DATABASE.engine.connect() as connection:
        for namespace, table in SCHEDULER_JOB_TABLES.items():
            for job_id, timestamp in connection.execute(
                    select(table.c.id, table.c.next_run_time).order_by(table.c.id)):
                schedules.append({
                    "namespace": namespace,
                    "id": job_id,
                    "next_run_time": (datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
                                      if timestamp is not None else None),
                })
    return schedules


def _release_database_session(func):
    @wraps(func)
    def run(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        finally:
            # APScheduler 会复用工作线程，因此必须在此显式移除 scoped_session，
            # 否则会话会在不同任务之间继续保留。
            DATABASE.close_session()

    return run


class DatabaseSessionBackgroundScheduler(BackgroundScheduler):
    """Background scheduler that returns each job thread's DB connection."""

    def add_job(self, func, *args, **kwargs):
        if callable(func):
            func = _release_database_session(func)
        return super().add_job(func, *args, **kwargs)


def run_persistent_job(reference, args=(), kwargs=None):
    """按模块入口恢复业务实例，JobStore 不保存实例、锁或数据库连接。"""
    try:
        module_name, path = reference.split(":", 1)
        module = import_module(module_name)
        if "." in path:
            factory_name, method_name = path.split(".", 1)
            function = getattr(getattr(module, factory_name)(), method_name)
        else:
            function = getattr(module, path)
        return function(*args, **(kwargs or {}))
    finally:
        DATABASE.close_session()


class DatabaseJobStore(SQLAlchemyJobStore):
    """使用活动业务数据库和统一 metadata，关闭时不销毁共享连接池。"""

    def __init__(self, namespace):
        BaseJobStore.__init__(self)
        self.pickle_protocol = pickle.HIGHEST_PROTOCOL
        self.jobs_t = SCHEDULER_JOB_TABLES[namespace]

    @property
    def engine(self):
        # 空间回收等操作会重建连接池，始终使用管理器当前的 engine。
        return DATABASE.engine

    def shutdown(self):
        # engine 归 DatabaseManager 管理，其他调度器和请求仍在使用它。
        pass


class PersistentBackgroundScheduler(BackgroundScheduler):
    """暂停加载持久任务，合并当前配置后恢复运行，保留已有 next_run_time。"""

    def __init__(self, namespace, **options):
        defaults = {"max_instances": 1, "coalesce": True, "misfire_grace_time": 300}
        defaults.update(options.pop("job_defaults", {}))
        super().__init__(jobstores={"default": DatabaseJobStore(namespace)},
                         job_defaults=defaults, **options)
        self._configured_ids = set()

    def start(self, paused=True):
        with _jobstore_access():
            return super().start(paused=paused)

    def _process_jobs(self):
        if self.state != STATE_RUNNING:
            return None
        try:
            with _jobstore_access():
                return super()._process_jobs()
        except Exception as error:
            # 维护或暂时断连时等待重试，调度线程不能因此退出。
            self._logger.warning("持久化定时任务暂不能访问数据库，将重试：%s", type(error).__name__)
            return self.jobstore_retry_interval

    def add_interval_job(self, reference, *, id, args=(), kwargs=None,
                         next_run_time=undefined, **interval):
        if self.state == STATE_STOPPED:
            raise RuntimeError("持久化调度器需要先以暂停状态启动，再同步任务")
        trigger = IntervalTrigger(timezone=self.timezone, **interval)
        arguments = (reference, tuple(args), dict(kwargs or {}))
        with _jobstore_access(), self._jobstores_lock:
            existing = self.get_job(id)
            if existing:
                # modify_job 不重新计算下一次时间；该次执行后再按新间隔递增。
                job = self.modify_job(id, trigger=trigger, func=run_persistent_job,
                                      args=arguments, kwargs={})
            else:
                options = {} if next_run_time is undefined else {"next_run_time": next_run_time}
                job = self.add_job(run_persistent_job, trigger, id=id, name=id,
                                   args=arguments, **options)
            self._configured_ids.add(id)
            return job

    def reconcile_jobs(self):
        """仅移除当前配置已删除或停用的任务，停止服务不会清空 JobStore。"""
        with _jobstore_access(), self._jobstores_lock:
            for job in self.get_jobs():
                if job.id not in self._configured_ids:
                    self.remove_job(job.id)
