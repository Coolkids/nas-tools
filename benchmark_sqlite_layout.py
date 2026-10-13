#!/usr/bin/env python3
"""Compare SQLite dual-file and merged-file layouts with the same app-shaped workload.

This is a local synthetic benchmark, not a production acceptance test. It uses
direct sqlite3 connections so it measures file layout and SQLite contention,
not SQLAlchemy pool checkout behavior. It never reads or modifies the active
NAS-Tools configuration or database.
"""

import argparse
import hashlib
import json
import multiprocessing
import os
import queue
import sqlite3
import tempfile
import threading
import time


def _percentile(values, percentile):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _connect(path):
    connection = sqlite3.connect(path, timeout=5)
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA wal_autocheckpoint=1000")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _create_layout(root, layout, media_items, history_items, ai_bytes):
    if layout == "dual_legacy_index":
        main_path = os.path.join(root, "user.db")
        media_path = os.path.join(root, "media.db")
    else:
        main_path = media_path = os.path.join(root, "nastools.db")
    optimized = layout == "merged_optimized"

    main = sqlite3.connect(main_path)
    media = main if media_path == main_path else sqlite3.connect(media_path)
    try:
        main.execute("PRAGMA journal_mode=WAL")
        if media is not main:
            media.execute("PRAGMA journal_mode=WAL")
        main.executescript("""
            CREATE TABLE SYSTEM_DICT (
                ID INTEGER PRIMARY KEY, TYPE TEXT, KEY TEXT, VALUE TEXT,
                UNIQUE(TYPE, KEY)
            );
            CREATE TABLE RECOGNITION_REQUEST (
                REQUEST_ID TEXT PRIMARY KEY, SOURCE TEXT, CREATED_AT TEXT,
                STATUS_SUMMARY TEXT, ORIGINAL_NAME TEXT, OVERALL_RESULT TEXT
            );
            CREATE INDEX IDX_RECOG_SOURCE_CREATED_REQUEST
                ON RECOGNITION_REQUEST(SOURCE, CREATED_AT, REQUEST_ID);
            CREATE INDEX IDX_RECOG_STATUS_SUMMARY
                ON RECOGNITION_REQUEST(STATUS_SUMMARY);
        """)
        if not optimized:
            main.executescript("""
                DROP INDEX IDX_RECOG_SOURCE_CREATED_REQUEST;
                CREATE INDEX IDX_RECOG_SOURCE ON RECOGNITION_REQUEST(SOURCE);
                CREATE INDEX IDX_RECOG_CREATED ON RECOGNITION_REQUEST(CREATED_AT);
            """)
        if media is main:
            main.executescript("""
                CREATE TABLE MEDIASYNC_ITEMS (
                    ID INTEGER PRIMARY KEY, SERVER TEXT, GENERATION_ID TEXT NOT NULL,
                    ITEM_ID TEXT, ITEM_TYPE TEXT, TITLE TEXT, YEAR TEXT, PATH TEXT, JSON TEXT,
                    UNIQUE(SERVER, GENERATION_ID, ITEM_ID)
                );
            """)
            if optimized:
                main.execute("""CREATE INDEX IDX_MEDIASYNC_SERVER_GENERATION_TITLE
                    ON MEDIASYNC_ITEMS(SERVER, GENERATION_ID, TITLE, YEAR)""")
            else:
                main.execute("""CREATE INDEX IDX_MEDIASYNC_SERVER_TITLE
                    ON MEDIASYNC_ITEMS(SERVER, TITLE, YEAR)""")
        else:
            media.executescript("""
                CREATE TABLE MEDIASYNC_ITEMS (
                    ID INTEGER PRIMARY KEY, SERVER TEXT, GENERATION_ID TEXT NOT NULL,
                    ITEM_ID TEXT, ITEM_TYPE TEXT, TITLE TEXT, YEAR TEXT, PATH TEXT, JSON TEXT,
                    UNIQUE(SERVER, GENERATION_ID, ITEM_ID)
                );
            """)
            if optimized:
                media.execute("""CREATE INDEX IDX_MEDIASYNC_SERVER_GENERATION_TITLE
                    ON MEDIASYNC_ITEMS(SERVER, GENERATION_ID, TITLE, YEAR)""")
            else:
                media.execute("""CREATE INDEX IDX_MEDIASYNC_SERVER_TITLE
                    ON MEDIASYNC_ITEMS(SERVER, TITLE, YEAR)""")

        main_payload = "x" * ai_bytes
        main.executemany(
            "INSERT INTO RECOGNITION_REQUEST VALUES (?, ?, ?, ?, ?, ?)",
            ((f"request-{index:08d}", f"source-{index % 8}",
              f"2026-01-{index % 28 + 1:02d}T12:00:00", "success",
              f"示例媒体-{index}", main_payload)
             for index in range(history_items)))
        main.executemany(
            "INSERT INTO SYSTEM_DICT(TYPE, KEY, VALUE) VALUES (?, ?, ?)",
            (("benchmark", f"setting-{index}", f"value-{index}")
             for index in range(min(history_items, 5000))))
        media.executemany(
            "INSERT INTO MEDIASYNC_ITEMS"
            "(SERVER, GENERATION_ID, ITEM_ID, ITEM_TYPE, TITLE, YEAR, PATH, JSON)"
            " VALUES (?, 'legacy', ?, ?, ?, ?, ?, ?)",
            ((f"server-{index % 4}", f"item-{index:08d}",
              "Movie" if index % 3 else "Series", f"标题-{index:08d}",
              str(1980 + index % 45), f"/library/{index:08d}", "{}")
             for index in range(media_items)))
        main.commit()
        if media is not main:
            media.commit()
    finally:
        if media is not main:
            media.close()
        main.close()

    return main_path, media_path


def _current_rss_bytes():
    try:
        with open("/proc/self/status", encoding="ascii") as stream:
            for line in stream:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return None


def _io_bytes():
    values = {}
    try:
        with open("/proc/self/io", encoding="ascii") as stream:
            for line in stream:
                name, value = line.split(":", 1)
                if name in {"read_bytes", "write_bytes"}:
                    values[name] = int(value)
    except OSError:
        return None
    return values


def _request_drop_file_cache(path):
    """Best-effort request to evict a private benchmark file from Linux page cache."""
    advice = getattr(os, "POSIX_FADV_DONTNEED", None)
    if advice is None or not hasattr(os, "posix_fadvise"):
        return False
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.posix_fadvise(descriptor, 0, 0, advice)
        finally:
            os.close(descriptor)
        return True
    except OSError:
        return False


def _generation_digest(connection, generation):
    digest = hashlib.sha256()
    count = 0
    cursor = connection.execute("""SELECT SERVER, ITEM_ID, ITEM_TYPE, TITLE, YEAR, PATH, JSON
        FROM MEDIASYNC_ITEMS WHERE GENERATION_ID=? ORDER BY SERVER, ITEM_ID""",
                                (generation,))
    try:
        while True:
            rows = cursor.fetchmany(1000)
            if not rows:
                break
            for row in rows:
                digest.update(json.dumps(row, ensure_ascii=False,
                                         separators=(",", ":")).encode("utf-8"))
                digest.update(b"\n")
                count += 1
    finally:
        cursor.close()
    return count, digest.hexdigest()


def _query_plan(connection, statement, parameters):
    return [str(row[-1]) for row in connection.execute(
        "EXPLAIN QUERY PLAN " + statement, parameters).fetchall()]


def _run_scenario(root, layout, duration, readers, batch_size,
                  media_items, history_items, ai_bytes, intervals_ms, results):
    main_path, media_path = _create_layout(
        root, layout, media_items, history_items, ai_bytes)
    stop = threading.Event()
    lock = threading.Lock()
    connections = threading.local()
    latencies = {"media_page": [], "history_page": [], "history_export": [],
                 "history_write": [], "subscription_update": [],
                 "media_sync_batch": [], "history_cleanup": []}
    errors = {"busy": 0, "other": 0}
    counters = {name: 0 for name in latencies}
    write_transactions = {"history_write": [], "subscription_update": [],
                          "media_sync_batch": [], "history_cleanup": []}
    next_id = 0
    id_lock = threading.Lock()
    sync_last_id = 0
    sync_passes = 0
    wal_peak = 0

    # Flush SQLite's WAL, then request (but cannot guarantee) eviction of these
    # private files before recording one first-read sample per logical domain.
    for path in {main_path, media_path}:
        checkpoint = sqlite3.connect(path, timeout=5)
        try:
            checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        finally:
            checkpoint.close()
    cold_read_ms = {}
    file_cache_drop_requested = {}
    for name, path, statement, parameters in (
        ("history_page", main_path,
         """SELECT REQUEST_ID, STATUS_SUMMARY, ORIGINAL_NAME
            FROM RECOGNITION_REQUEST WHERE SOURCE=? AND STATUS_SUMMARY=?
            ORDER BY CREATED_AT, REQUEST_ID LIMIT 50""", ("source-3", "success")),
        ("media_page", media_path,
         """SELECT ID, ITEM_ID, TITLE, YEAR FROM MEDIASYNC_ITEMS
            WHERE SERVER=? AND GENERATION_ID='legacy'
            ORDER BY TITLE, YEAR, ID LIMIT 50""", ("server-1",)),
    ):
        file_cache_drop_requested[name] = _request_drop_file_cache(path)
        connection = _connect(path)
        started = time.perf_counter()
        try:
            connection.execute(statement, parameters).fetchall()
            cold_read_ms[name] = round((time.perf_counter() - started) * 1000, 3)
        finally:
            connection.close()

    # Warm the file and representative indexes before collecting hot-read data.
    warm_main = _connect(main_path)
    warm_media = warm_main if media_path == main_path else _connect(media_path)
    try:
        history_query = """SELECT REQUEST_ID, STATUS_SUMMARY, ORIGINAL_NAME
            FROM RECOGNITION_REQUEST WHERE SOURCE=? AND STATUS_SUMMARY=?
            ORDER BY CREATED_AT, REQUEST_ID LIMIT 50"""
        history_parameters = ("source-3", "success")
        media_query = """SELECT ID, ITEM_ID, TITLE, YEAR
            FROM MEDIASYNC_ITEMS WHERE SERVER=? AND GENERATION_ID='legacy'
            ORDER BY TITLE, YEAR, ID LIMIT 50"""
        media_parameters = ("server-1",)
        warm_main.execute(history_query, history_parameters).fetchall()
        warm_media.execute(media_query, media_parameters).fetchall()
        query_plans = {
            "history_page": _query_plan(warm_main, history_query, history_parameters),
            "media_page": _query_plan(warm_media, media_query, media_parameters),
        }
    finally:
        if warm_media is not warm_main:
            warm_media.close()
        warm_main.close()

    def operation(name, action, write=False):
        nonlocal wal_peak
        started = time.perf_counter()
        try:
            action()
        except sqlite3.OperationalError as error:
            message = str(error).lower()
            with lock:
                if "locked" in message or "busy" in message:
                    errors["busy"] += 1
                else:
                    errors["other"] += 1
        except Exception:
            with lock:
                errors["other"] += 1
        else:
            elapsed = (time.perf_counter() - started) * 1000
            with lock:
                latencies[name].append(elapsed)
                counters[name] += 1
        if write:
            for path in {main_path, media_path}:
                try:
                    observed = os.path.getsize(path + "-wal")
                except OSError:
                    continue
                with lock:
                    wal_peak = max(wal_peak, observed)

    def write_transaction(connection, name, action):
        waiting_started = time.perf_counter()
        connection.execute("BEGIN IMMEDIATE")
        transaction_started = time.perf_counter()
        try:
            action()
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        committed_at = time.perf_counter()
        with lock:
            write_transactions[name].append({
                "lock_wait_ms": (transaction_started - waiting_started) * 1000,
                "transaction_ms": (committed_at - transaction_started) * 1000,
            })

    def worker_connections():
        if not hasattr(connections, "main"):
            connections.main = _connect(main_path)
            connections.media = (connections.main if media_path == main_path
                                 else _connect(media_path))
        return connections.main, connections.media

    def media_page():
        _main, media = worker_connections()
        cursor = media.execute("""SELECT ID, ITEM_ID, TITLE, YEAR
            FROM MEDIASYNC_ITEMS
            WHERE SERVER=? AND GENERATION_ID='legacy'
            ORDER BY TITLE, YEAR, ID LIMIT 50 OFFSET ?""",
                              ("server-1", (time.time_ns() // 1_000_000) %
                               max(1, media_items // 4)))
        cursor.fetchall()

    def history_page():
        main, _media = worker_connections()
        cursor = main.execute("""SELECT REQUEST_ID, STATUS_SUMMARY, ORIGINAL_NAME
            FROM RECOGNITION_REQUEST
            WHERE SOURCE=? AND STATUS_SUMMARY=?
            ORDER BY CREATED_AT, REQUEST_ID LIMIT 50""",
                              ("source-3", "success"))
        cursor.fetchall()

    def history_export():
        main, _media = worker_connections()
        cursor = main.execute("""SELECT REQUEST_ID, ORIGINAL_NAME, OVERALL_RESULT
            FROM RECOGNITION_REQUEST ORDER BY REQUEST_ID""")
        try:
            while cursor.fetchmany(250):
                pass
        finally:
            cursor.close()

    def history_write():
        main, _media = worker_connections()
        nonlocal next_id
        with id_lock:
            next_id += 1
            identifier = next_id
        write_transaction(main, "history_write", lambda: main.execute(
            "INSERT INTO RECOGNITION_REQUEST VALUES (?, ?, ?, ?, ?, ?)",
            (f"live-{identifier:08d}", "source-live",
             f"2026-10-13T12:{identifier % 60:02d}:00", "success",
             f"new-history-{identifier}", "z" * ai_bytes)))

    def subscription_update():
        main, _media = worker_connections()
        nonlocal next_id
        with id_lock:
            next_id += 1
            identifier = next_id
        write_transaction(main, "subscription_update", lambda: main.execute(
            """INSERT INTO SYSTEM_DICT(TYPE, KEY, VALUE)
                VALUES ('subscribe', 'benchmark-subscription', ?)
                ON CONFLICT(TYPE, KEY) DO UPDATE SET VALUE=excluded.VALUE""",
            (f"updated-{identifier}",)))

    def media_sync_batch():
        _main, media = worker_connections()
        nonlocal sync_last_id, sync_passes
        rows = media.execute("""SELECT SERVER, ITEM_ID, ITEM_TYPE, TITLE, YEAR, PATH, JSON, ID
            FROM MEDIASYNC_ITEMS WHERE GENERATION_ID='legacy' AND ID > ?
            ORDER BY ID LIMIT ?""", (sync_last_id, batch_size)).fetchall()
        if not rows:
            sync_last_id = 0
            sync_passes += 1
            rows = media.execute("""SELECT SERVER, ITEM_ID, ITEM_TYPE, TITLE, YEAR, PATH, JSON, ID
                FROM MEDIASYNC_ITEMS WHERE GENERATION_ID='legacy'
                ORDER BY ID LIMIT ?""", (batch_size,)).fetchall()
        if not rows:
            return
        sync_last_id = rows[-1]["ID"] if isinstance(rows[-1], sqlite3.Row) else rows[-1][-1]
        write_transaction(media, "media_sync_batch", lambda: media.executemany(
            """INSERT INTO MEDIASYNC_ITEMS
                (SERVER, GENERATION_ID, ITEM_ID, ITEM_TYPE, TITLE, YEAR, PATH, JSON)
                VALUES (?, 'new-generation', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(SERVER, GENERATION_ID, ITEM_ID)
                DO UPDATE SET ITEM_TYPE=excluded.ITEM_TYPE, TITLE=excluded.TITLE,
                    YEAR=excluded.YEAR, PATH=excluded.PATH, JSON=excluded.JSON""",
            (tuple(row[:-1]) for row in rows)))

    def history_cleanup():
        main, _media = worker_connections()
        write_transaction(main, "history_cleanup", lambda: main.execute(
            """DELETE FROM RECOGNITION_REQUEST WHERE REQUEST_ID IN (
                SELECT REQUEST_ID FROM RECOGNITION_REQUEST
                WHERE REQUEST_ID LIKE 'live-%' ORDER BY REQUEST_ID LIMIT 20
            )"""))

    def worker(name, action, write=False, interval_ms=0):
        try:
            while not stop.is_set():
                operation(name, action, write)
                if interval_ms > 0:
                    stop.wait(interval_ms / 1000)
        finally:
            if hasattr(connections, "main"):
                if connections.media is not connections.main:
                    connections.media.close()
                connections.main.close()

    before_io = _io_bytes()
    before_cpu = time.process_time()
    before_rss = _current_rss_bytes()
    workers = []
    for index in range(readers):
        name, action = (("media_page", media_page) if index % 2 == 0
                        else ("history_page", history_page))
        workers.append(threading.Thread(
            target=worker, args=(name, action, False, intervals_ms["reader"]), daemon=True))
    workers.extend([
        threading.Thread(target=worker,
                         args=("history_export", history_export, False,
                               intervals_ms["reader"] * 5), daemon=True),
        threading.Thread(target=worker, args=("history_write", history_write, True,
                                              intervals_ms["history_write"]), daemon=True),
        threading.Thread(target=worker, args=("subscription_update", subscription_update, True,
                                              intervals_ms["subscription_update"]), daemon=True),
        threading.Thread(target=worker, args=("media_sync_batch", media_sync_batch, True,
                                              intervals_ms["media_batch"]), daemon=True),
        threading.Thread(target=worker, args=("history_cleanup", history_cleanup, True,
                                              intervals_ms["history_cleanup"]), daemon=True),
    ])
    started = time.perf_counter()
    for thread in workers:
        thread.start()
    time.sleep(duration)
    stop.set()
    for thread in workers:
        thread.join(timeout=15)
    alive_workers = [thread.name for thread in workers if thread.is_alive()]
    if alive_workers:
        raise RuntimeError(f"benchmark workers did not stop: {alive_workers}")
    elapsed = time.perf_counter() - started

    unique_paths = {main_path, media_path}
    validation_connection = sqlite3.connect(media_path)
    try:
        source_count, source_digest = _generation_digest(validation_connection, "legacy")
        target_count, target_digest = _generation_digest(validation_connection, "new-generation")
    finally:
        validation_connection.close()
    sync_data_match = (source_count, source_digest) == (target_count, target_digest)
    if sync_passes and not sync_data_match:
        raise RuntimeError("completed synthetic media sync pass failed count/digest validation")
    for path in unique_paths:
        connection = sqlite3.connect(path, timeout=5, isolation_level=None)
        try:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            connection.close()
        except sqlite3.Error:
            connection.close()
    database_bytes = sum(os.path.getsize(path) for path in unique_paths if os.path.exists(path))
    wal_bytes = sum(os.path.getsize(path + "-wal") for path in unique_paths
                    if os.path.exists(path + "-wal"))
    after_io = _io_bytes()
    result = {
        "layout": layout,
        "duration_seconds": round(elapsed, 3),
        "readers": readers,
        "operation_intervals_ms": intervals_ms,
        "operations": {},
        "query_plans": query_plans,
        "errors": errors,
        "database_bytes_after_checkpoint": database_bytes,
        "wal_bytes_after_checkpoint": wal_bytes,
        "peak_wal_bytes_observed": wal_peak,
        "completed_media_sync_passes": sync_passes,
        "media_sync_validation": {"source_rows": source_count,
                                  "target_rows": target_count,
                                  "content_match": sync_data_match},
        "first_read_after_cache_drop_request_ms": cold_read_ms,
        "file_cache_drop_requested": file_cache_drop_requested,
        "cpu_seconds": round(time.process_time() - before_cpu, 3),
        "rss_bytes_before": before_rss,
        "rss_bytes_after": _current_rss_bytes(),
        "io_bytes_delta": ({key: after_io.get(key, 0) - before_io.get(key, 0)
                             for key in ("read_bytes", "write_bytes")}
                            if before_io and after_io else None),
    }
    for name, samples in latencies.items():
        result["operations"][name] = {
            "count": counters[name],
            "per_second": round(counters[name] / elapsed, 2),
            "p50_ms": round(_percentile(samples, 0.50), 3) if samples else None,
            "p95_ms": round(_percentile(samples, 0.95), 3) if samples else None,
            "p99_ms": round(_percentile(samples, 0.99), 3) if samples else None,
            "max_ms": round(max(samples), 3) if samples else None,
        }
    result["write_transactions"] = {}
    for name, samples in write_transactions.items():
        result["write_transactions"][name] = {
            "count": len(samples),
            "lock_wait_ms": {
                "p50": round(_percentile([sample["lock_wait_ms"] for sample in samples], 0.50), 3)
                if samples else None,
                "p95": round(_percentile([sample["lock_wait_ms"] for sample in samples], 0.95), 3)
                if samples else None,
                "max": round(max(sample["lock_wait_ms"] for sample in samples), 3)
                if samples else None,
            },
            "transaction_ms": {
                "p50": round(_percentile([sample["transaction_ms"] for sample in samples], 0.50), 3)
                if samples else None,
                "p95": round(_percentile([sample["transaction_ms"] for sample in samples], 0.95), 3)
                if samples else None,
                "max": round(max(sample["transaction_ms"] for sample in samples), 3)
                if samples else None,
            },
        }
    results.put(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=5,
                        help="seconds per layout (default: 5)")
    parser.add_argument("--readers", type=int, default=4)
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--media-items", type=int, default=20000)
    parser.add_argument("--history-items", type=int, default=5000)
    parser.add_argument("--ai-bytes", type=int, default=8192)
    parser.add_argument("--reader-interval-ms", type=int, default=0,
                        help="delay between page reads/exports; zero saturates readers")
    parser.add_argument("--history-write-interval-ms", type=int, default=0,
                        help="delay between recognition writes; zero saturates writer")
    parser.add_argument("--subscription-update-interval-ms", type=int, default=0,
                        help="delay between subscription updates; zero saturates writer")
    parser.add_argument("--media-batch-interval-ms", type=int, default=0,
                        help="delay between bounded source-copy batches; zero saturates sync")
    parser.add_argument("--history-cleanup-interval-ms", type=int, default=0,
                        help="delay between history cleanup batches; zero saturates cleanup")
    parser.add_argument("--output", help="optional JSON output path")
    args = parser.parse_args()
    if min(args.duration, args.readers, args.batch_size,
           args.media_items, args.history_items, args.ai_bytes) <= 0:
        parser.error("all numeric workload options must be positive")
    intervals_ms = {"reader": args.reader_interval_ms,
                    "history_write": args.history_write_interval_ms,
                    "subscription_update": args.subscription_update_interval_ms,
                    "media_batch": args.media_batch_interval_ms,
                    "history_cleanup": args.history_cleanup_interval_ms}
    if min(intervals_ms.values()) < 0:
        parser.error("operation intervals must be zero or positive")

    context = multiprocessing.get_context("spawn")
    report = {
        "python": os.sys.version.split()[0],
        "sqlite": sqlite3.sqlite_version,
        "parameters": vars(args),
        "note": "synthetic sqlite3 workload; dual/merged legacy-index profiles isolate merge effect; optimized merged profile adds composite indexes; not app pool, production data, or scheduled-sync acceptance",
        "cache_note": "first-read timings use best-effort POSIX_FADV_DONTNEED; hot-read percentiles follow explicit warm-up",
        "load_note": "zero operation intervals offer saturated work; nonzero intervals control offered workload",
        "results": [],
    }
    with tempfile.TemporaryDirectory(prefix="nastools-sqlite-layout-") as directory:
        for layout in ("dual_legacy_index", "merged_legacy_index", "merged_optimized"):
            root = os.path.join(directory, layout)
            os.makedirs(root)
            results = context.Queue()
            process = context.Process(
                target=_run_scenario,
                args=(root, layout, args.duration, args.readers, args.batch_size,
                      args.media_items, args.history_items, args.ai_bytes,
                      intervals_ms, results))
            process.start()
            deadline = time.monotonic() + args.duration + 120
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    process.terminate()
                    process.join(5)
                    raise RuntimeError(f"{layout} benchmark worker timed out")
                try:
                    result = results.get(timeout=min(5, remaining))
                    break
                except queue.Empty:
                    if not process.is_alive():
                        raise RuntimeError(
                            f"{layout} benchmark worker exited {process.exitcode} without a report")
            process.join(15)
            if process.is_alive():
                process.terminate()
                process.join(5)
                raise RuntimeError(f"{layout} benchmark worker did not exit")
            if process.exitcode:
                raise RuntimeError(f"{layout} benchmark worker exited {process.exitcode}")
            report["results"].append(result)
            results.close()

    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as stream:
            stream.write(rendered + "\n")
    print(rendered)


if __name__ == "__main__":
    main()
