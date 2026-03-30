from __future__ import annotations

import importlib
import json
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile
import unittest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _save_version_worker(
    data_dir: str,
    start_event,
    result_queue,
    worker_id: int,
) -> None:
    os.environ["SHIFT_APP_DATA_DIR"] = data_dir
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    history_store = importlib.import_module("history_store")
    start_event.wait()
    version = history_store.save_schedule_version(
        "2026-03-19",
        {"target_date": "2026-03-19", "worker_id": worker_id},
        {"table": [{"worker_id": worker_id}]},
    )
    result_queue.put(version)


class HistoryStoreConcurrencyTests(unittest.TestCase):
    def test_save_schedule_version_serializes_concurrent_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ctx = multiprocessing.get_context("spawn")
            start_event = ctx.Event()
            result_queue = ctx.Queue()
            workers = [
                ctx.Process(
                    target=_save_version_worker,
                    args=(tmpdir, start_event, result_queue, worker_id),
                )
                for worker_id in range(4)
            ]

            for worker in workers:
                worker.start()

            start_event.set()

            versions = sorted(result_queue.get(timeout=10) for _ in workers)

            for worker in workers:
                worker.join(timeout=10)
                self.assertEqual(worker.exitcode, 0)

            history_path = Path(tmpdir) / "schedule_history.json"
            with history_path.open("r", encoding="utf-8") as fh:
                history = json.load(fh)

            self.assertEqual(versions, [1, 2, 3, 4])
            self.assertEqual(
                sorted(item["version"] for item in history),
                [1, 2, 3, 4],
            )
