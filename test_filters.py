#!/usr/bin/env python3
"""
Tests for SNR threshold, DM threshold, and burst safeguard filtering
in TriggerBufferWorker.

Run:
    python3 test_filters.py
"""

import collections
import os
import queue
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("TRIGGER_SECURE_KEY", "test_key")

from udp_to_triggerbuffer import TriggerBufferWorker, UdpTask


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_task(sn=25.0, dm=200.0, cand_mjd=60000.0):
    fields = (sn, 1000, 0.5, 0, 0, dm, 1, cand_mjd)
    return UdpTask(addr=("127.0.0.1", 9999), raw_text="", fields=fields)


def make_worker(q, stop, **kwargs):
    defaults = dict(
        name="test",
        in_queue=q,
        stop_event=stop,
        endpoint="http://fake/trigger",
        project_id="C001",
        secure_key_env="TRIGGER_SECURE_KEY",
        past_seconds=120,
        obstime=600,
        pretty=False,
        pretend=True,
        use_start_time_zero=True,
        min_trigger_interval_sec=0,   # disable interval limit for most tests
        min_sn=20.0,
        min_dm=100.0,
        burst_window_sec=1.0,
        burst_max_count=5,
        verbose=False,
    )
    defaults.update(kwargs)
    return TriggerBufferWorker(**defaults)


def run_worker_drain(worker, q, tasks, timeout=2.0):
    """Put tasks in queue, start worker, wait until queue is drained, stop."""
    for t in tasks:
        q.put(t)
    worker.start()
    q.join()
    worker.stop_event.set()
    worker.join(timeout=timeout)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSnrFilter(unittest.TestCase):

    @patch("requests.Session.get")
    def test_below_threshold_not_triggered(self, mock_get):
        q = queue.Queue()
        stop = threading.Event()
        worker = make_worker(q, stop, min_sn=20.0, min_dm=0.0, burst_max_count=0)
        run_worker_drain(worker, q, [make_task(sn=10.0)])
        mock_get.assert_not_called()

    @patch("requests.Session.get")
    def test_above_threshold_triggered(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200)
        q = queue.Queue()
        stop = threading.Event()
        worker = make_worker(q, stop, min_sn=20.0, min_dm=0.0, burst_max_count=0)
        run_worker_drain(worker, q, [make_task(sn=25.0)])
        mock_get.assert_called_once()

    @patch("requests.Session.get")
    def test_exactly_at_threshold_triggered(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200)
        q = queue.Queue()
        stop = threading.Event()
        worker = make_worker(q, stop, min_sn=20.0, min_dm=0.0, burst_max_count=0)
        run_worker_drain(worker, q, [make_task(sn=20.0)])
        mock_get.assert_called_once()


class TestDmFilter(unittest.TestCase):

    @patch("requests.Session.get")
    def test_below_threshold_not_triggered(self, mock_get):
        q = queue.Queue()
        stop = threading.Event()
        worker = make_worker(q, stop, min_sn=0.0, min_dm=100.0, burst_max_count=0)
        run_worker_drain(worker, q, [make_task(dm=50.0)])
        mock_get.assert_not_called()

    @patch("requests.Session.get")
    def test_above_threshold_triggered(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200)
        q = queue.Queue()
        stop = threading.Event()
        worker = make_worker(q, stop, min_sn=0.0, min_dm=100.0, burst_max_count=0)
        run_worker_drain(worker, q, [make_task(dm=200.0)])
        mock_get.assert_called_once()

    @patch("requests.Session.get")
    def test_both_filters_must_pass(self, mock_get):
        """High DM but low SNR — should not trigger."""
        q = queue.Queue()
        stop = threading.Event()
        worker = make_worker(q, stop, min_sn=20.0, min_dm=100.0, burst_max_count=0)
        run_worker_drain(worker, q, [make_task(sn=5.0, dm=300.0)])
        mock_get.assert_not_called()


class TestBurstFilter(unittest.TestCase):

    def test_is_burst_triggers_after_limit(self):
        q = queue.Queue()
        stop = threading.Event()
        worker = make_worker(q, stop, burst_window_sec=1.0, burst_max_count=5)
        results = [worker._is_burst() for _ in range(7)]
        # first 5 calls: not burst; 6th and 7th: burst
        self.assertFalse(any(results[:5]))
        self.assertTrue(all(results[5:]))

    def test_burst_resets_after_window(self):
        q = queue.Queue()
        stop = threading.Event()
        worker = make_worker(q, stop, burst_window_sec=0.1, burst_max_count=3)
        for _ in range(5):
            worker._is_burst()
        time.sleep(0.15)
        # window has expired — should no longer be in burst
        self.assertFalse(worker._is_burst())

    @patch("requests.Session.get")
    def test_burst_suppresses_trigger(self, mock_get):
        """Send 10 tasks rapidly; only the first burst_max_count should go through."""
        mock_get.return_value = MagicMock(status_code=200)
        q = queue.Queue()
        stop = threading.Event()
        burst_max = 3
        worker = make_worker(
            q, stop,
            min_sn=0.0, min_dm=0.0,
            burst_window_sec=5.0,
            burst_max_count=burst_max,
            min_trigger_interval_sec=0,
        )
        tasks = [make_task() for _ in range(10)]
        run_worker_drain(worker, q, tasks)
        # only burst_max triggers should have fired
        self.assertEqual(mock_get.call_count, burst_max)


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
