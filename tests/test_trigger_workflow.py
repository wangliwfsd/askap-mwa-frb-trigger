#!/usr/bin/env python3
"""
Tests for SNR threshold, DM threshold, and burst safeguard filtering
in TriggerBufferWorker.

Run:
    python3 -m unittest discover -s tests -v
"""

import collections
import csv
import json
import os
from pathlib import Path
import queue
import threading
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

os.environ.setdefault("TRIGGER_SECURE_KEY", "test_key")

from udp_to_triggerbuffer import TriggerBufferWorker, UdpTask


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_task(sn=25.0, dm=200.0, cand_mjd=60000.0):
    fields = (sn, 1000, 0.5, 0, 0, dm, 1, cand_mjd)
    return UdpTask(addr=("127.0.0.1", 9999), raw_text="", fields=fields)


def json_response(data, status=200):
    response = MagicMock(status_code=status)
    response.text = json.dumps(data)
    response.json.return_value = data
    return response


def successful_workflow_responses(trigger_id_base=100):
    return [
        json_response({"success": True, "trigger_id": trigger_id_base}),
        json_response([{
            "id": trigger_id_base,
            "success": True,
            "trigger_mode": "BUFFER",
            "obsids": [123],
        }]),
        json_response(False),
        json_response({"success": True, "trigger_id": trigger_id_base + 1}),
        json_response([{
            "id": trigger_id_base + 1,
            "success": True,
            "trigger_mode": "MWAX_VCS",
            "obsids": [456],
        }]),
    ]


def make_worker(q, stop, **kwargs):
    defaults = dict(
        name="test",
        in_queue=q,
        stop_event=stop,
        endpoint="http://fake/triggerbuffer",
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
        max_retries=0,
        verify_attempts=1,
        verify_delay_sec=0,
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
        with tempfile.TemporaryDirectory() as directory:
            csv_path = os.path.join(directory, "triggers.csv")
            worker = make_worker(
                q, stop, min_sn=20.0, min_dm=0.0, burst_max_count=0,
                trigger_csv=csv_path,
            )
            run_worker_drain(worker, q, [make_task(sn=10.0)])
            with open(csv_path, newline="", encoding="utf-8") as csv_file:
                rows = list(csv.DictReader(csv_file))
        mock_get.assert_not_called()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["service"], "candidate_filter")
        self.assertEqual(rows[0]["filter_status"], "rejected")
        self.assertEqual(
            rows[0]["filter_reason"], "sn_below_threshold: 10.00 < 20.00"
        )
        self.assertEqual(rows[0]["api_raw_responses"], "[]")

    @patch("requests.Session.get")
    def test_above_threshold_triggered(self, mock_get):
        mock_get.side_effect = successful_workflow_responses()
        q = queue.Queue()
        stop = threading.Event()
        worker = make_worker(q, stop, min_sn=20.0, min_dm=0.0, burst_max_count=0)
        run_worker_drain(worker, q, [make_task(sn=25.0)])
        self.assertEqual(mock_get.call_count, 5)

    @patch("requests.Session.get")
    def test_exactly_at_threshold_triggered(self, mock_get):
        mock_get.side_effect = successful_workflow_responses()
        q = queue.Queue()
        stop = threading.Event()
        worker = make_worker(q, stop, min_sn=20.0, min_dm=0.0, burst_max_count=0)
        run_worker_drain(worker, q, [make_task(sn=20.0)])
        self.assertEqual(mock_get.call_count, 5)


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
        mock_get.side_effect = successful_workflow_responses()
        q = queue.Queue()
        stop = threading.Event()
        worker = make_worker(q, stop, min_sn=0.0, min_dm=100.0, burst_max_count=0)
        run_worker_drain(worker, q, [make_task(dm=200.0)])
        self.assertEqual(mock_get.call_count, 5)

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
        mock_get.side_effect = sum(
            (successful_workflow_responses(100 + i * 2) for i in range(3)), []
        )
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
        self.assertEqual(mock_get.call_count, burst_max * 5)


class TestFilteredCandidateCsv(unittest.TestCase):

    def _reject_and_read(self, *, task=None, prepare=None, **worker_options):
        q = queue.Queue()
        stop = threading.Event()
        with tempfile.TemporaryDirectory() as directory:
            csv_path = os.path.join(directory, "triggers.csv")
            worker = make_worker(q, stop, trigger_csv=csv_path, **worker_options)
            if prepare is not None:
                prepare(worker)
            worker.process_task(task or make_task())
            worker.session.close()
            with open(csv_path, newline="", encoding="utf-8") as csv_file:
                rows = list(csv.DictReader(csv_file))
            mode = os.stat(csv_path).st_mode & 0o077
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["service"], "candidate_filter")
        self.assertEqual(rows[0]["filter_status"], "rejected")
        self.assertEqual(rows[0]["api_errors"], "")
        self.assertEqual(rows[0]["api_raw_responses"], "[]")
        self.assertEqual(rows[0]["history_raw_responses"], "[]")
        self.assertEqual(rows[0]["busy_raw_responses"], "[]")
        self.assertEqual(mode, 0)
        return rows[0]

    @patch("requests.Session.get")
    def test_old_csv_schema_is_backed_up_before_new_record(self, mock_get):
        q = queue.Queue()
        stop = threading.Event()
        with tempfile.TemporaryDirectory() as directory:
            csv_path = os.path.join(directory, "triggers.csv")
            old_contents = "old_column\nold_value\n"
            with open(csv_path, "w", encoding="utf-8") as csv_file:
                csv_file.write(old_contents)

            worker = make_worker(
                q, stop, trigger_csv=csv_path,
                min_sn=20, min_dm=0, burst_max_count=0,
            )
            worker.process_task(make_task(sn=10))
            worker.session.close()

            backups = list(Path(directory).glob("triggers.csv.*.bak"))
            with open(csv_path, newline="", encoding="utf-8") as csv_file:
                row = next(csv.DictReader(csv_file))

            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(), old_contents)
            self.assertEqual(backups[0].stat().st_mode & 0o077, 0)
            self.assertEqual(Path(csv_path).stat().st_mode & 0o077, 0)

        mock_get.assert_not_called()
        self.assertEqual(row["service"], "candidate_filter")
        self.assertEqual(row["filter_status"], "rejected")

    @patch("requests.Session.get")
    def test_dm_rejection_is_written_to_csv_without_http(self, mock_get):
        row = self._reject_and_read(
            task=make_task(dm=50.0), min_sn=0, min_dm=100, burst_max_count=0,
        )
        mock_get.assert_not_called()
        self.assertEqual(
            row["filter_reason"], "dm_below_threshold: 50.00 < 100.00"
        )

    @patch("requests.Session.get")
    def test_burst_rejection_is_written_to_csv_without_http(self, mock_get):
        def fill_burst_window(worker):
            self.assertFalse(worker._is_burst())

        row = self._reject_and_read(
            min_sn=0, min_dm=0, burst_max_count=1, burst_window_sec=5,
            prepare=fill_burst_window,
        )
        mock_get.assert_not_called()
        self.assertEqual(
            row["filter_reason"],
            "burst_safeguard: candidate count exceeded 1 within 5s",
        )

    @patch("requests.Session.get")
    def test_rate_limit_rejection_is_written_to_csv_without_http(self, mock_get):
        def set_recent_trigger(worker):
            worker._last_trigger_monotonic = time.monotonic()

        row = self._reject_and_read(
            min_sn=0, min_dm=0, burst_max_count=0,
            min_trigger_interval_sec=60, prepare=set_recent_trigger,
        )
        mock_get.assert_not_called()
        self.assertEqual(
            row["filter_reason"],
            "trigger_rate_limit: minimum interval is 60s",
        )


class TestMwaWorkflow(unittest.TestCase):

    def setUp(self):
        self.q = queue.Queue()
        self.stop = threading.Event()
        self.worker = make_worker(
            self.q,
            self.stop,
            min_sn=0,
            min_dm=0,
            burst_max_count=0,
            obstime=600,
            past_seconds=120,
            use_start_time_zero=False,
            show_endpoint="https://ws.mwatelescope.org/trigger/find",
        )

    def tearDown(self):
        self.worker.session.close()

    def test_urls_request_historical_buffer_and_all_sky_vcs(self):
        buffer_url = self.worker.build_triggerbuffer_url(make_task())
        buffer_params = parse_qs(urlsplit(buffer_url).query)
        self.assertEqual(
            int(buffer_params["end_time"][0]) - int(buffer_params["start_time"][0]),
            120,
        )
        self.assertNotIn("obstime", buffer_params)
        self.assertNotIn("pretty", buffer_params)

        vcs_url = self.worker.build_triggervcs_url()
        vcs_params = parse_qs(urlsplit(vcs_url).query)
        self.assertEqual(vcs_params["exptime"], ["600"])
        self.assertEqual(vcs_params["nobs"], ["1"])
        self.assertNotIn("pretty", vcs_params)
        for target_param in ("ra", "dec", "source", "alt", "az"):
            self.assertNotIn(target_param, vcs_params)

    @patch("requests.Session.get")
    def test_successful_ordered_workflow_and_history_verification(self, mock_get):
        mock_get.side_effect = successful_workflow_responses()
        self.worker.process_task(make_task())

        paths = [urlsplit(call.args[0]).path for call in mock_get.call_args_list]
        self.assertEqual(
            paths,
            ["/triggerbuffer", "/trigger/find", "/busy", "/triggervcs", "/trigger/find"],
        )
        history_queries = [
            parse_qs(urlsplit(mock_get.call_args_list[index].args[0]).query)
            for index in (1, 4)
        ]
        self.assertEqual(history_queries[0]["trigger_id"], ["100"])
        self.assertEqual(history_queries[1]["trigger_id"], ["101"])

    @patch("requests.Session.get")
    def test_audit_logs_include_ids_obsids_and_exclude_secure_key(self, mock_get):
        mock_get.side_effect = successful_workflow_responses()

        with self.assertLogs("askap_mwa_trigger", level="INFO") as captured:
            self.worker.process_task(make_task())

        logs = "\n".join(captured.output)
        self.assertIn("trigger_id=100", logs)
        self.assertIn("obsids=[123]", logs)
        self.assertIn('"id": 100', logs)
        self.assertIn('"trigger_mode": "BUFFER"', logs)
        self.assertNotIn("test_key", logs)

    @patch("requests.Session.get")
    def test_csv_records_buffer_and_vcs_without_secure_key(self, mock_get):
        mock_get.side_effect = successful_workflow_responses()

        with tempfile.TemporaryDirectory() as directory:
            csv_path = os.path.join(directory, "triggers.csv")
            self.worker.trigger_csv = csv_path
            self.worker.process_task(make_task())

            with open(csv_path, newline="", encoding="utf-8") as csv_file:
                rows = list(csv.DictReader(csv_file))
            csv_mode = os.stat(csv_path).st_mode

        self.assertEqual(csv_mode & 0o077, 0)
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            [(row["service"], row["trigger_id"]) for row in rows],
            [("triggerbuffer", "100"), ("triggervcs", "101")],
        )
        self.assertTrue(rows[0]["requested_utc"])
        self.assertTrue(rows[0]["recorded_utc"])
        self.assertEqual(rows[0]["api_errors"], "")
        self.assertEqual(rows[0]["filter_status"], "accepted")
        self.assertEqual(rows[0]["filter_reason"], "")
        self.assertEqual(rows[0]["history_mode"], "BUFFER")
        self.assertEqual(rows[1]["history_mode"], "MWAX_VCS")
        self.assertEqual(rows[0]["history_obsids"], "[123]")
        self.assertEqual(rows[1]["history_obsids"], "[456]")

        buffer_api_raw = json.loads(rows[0]["api_raw_responses"])
        buffer_history_raw = json.loads(rows[0]["history_raw_responses"])
        vcs_busy_raw = json.loads(rows[1]["busy_raw_responses"])
        self.assertEqual(buffer_api_raw[0]["http_status"], 200)
        self.assertIn('"trigger_id": 100', buffer_api_raw[0]["raw_response"])
        self.assertEqual(buffer_history_raw[0]["http_status"], 200)
        self.assertEqual(vcs_busy_raw[0]["raw_response"], "false")

        self.assertNotIn("secure_key", rows[0]["request_params"])
        self.assertNotIn("test_key", repr(rows))

    @patch("requests.Session.get")
    def test_csv_keeps_raw_responses_from_every_find_attempt(self, mock_get):
        self.worker.verify_attempts = 2
        mock_get.side_effect = [
            json_response({"success": True, "trigger_id": 300}),
            json_response([]),
            json_response([{
                "id": 300,
                "success": True,
                "trigger_mode": "BUFFER",
                "obsids": [789],
            }]),
        ]

        with tempfile.TemporaryDirectory() as directory:
            csv_path = os.path.join(directory, "triggers.csv")
            self.worker.trigger_csv = csv_path
            ok = self.worker._call_trigger(
                "triggerbuffer",
                self.worker.build_triggerbuffer_url(make_task()),
                "BUFFER",
                make_task(),
            )
            with open(csv_path, newline="", encoding="utf-8") as csv_file:
                row = next(csv.DictReader(csv_file))

        self.assertTrue(ok)
        find_raw = json.loads(row["history_raw_responses"])
        self.assertEqual(len(find_raw), 2)
        self.assertEqual(find_raw[0]["raw_response"], "[]")
        self.assertIn('"id": 300', find_raw[1]["raw_response"])

    @patch("requests.Session.get")
    def test_busy_still_attempts_vcs_and_logs_rejection_reason(self, mock_get):
        mock_get.side_effect = [
            json_response({"success": True, "trigger_id": 10}),
            json_response({
                "trigger_id": 10,
                "success": True,
                "trigger_mode": "BUFFER",
                "obsids": [123],
            }),
            json_response(True),
            json_response({
                "success": False,
                "trigger_id": 11,
                "errors": {"0": "protected scheduled observation"},
            }),
            json_response({
                "trigger_id": 11,
                "success": False,
                "trigger_mode": "MWAX_VCS",
                "errors": ["protected scheduled observation"],
                "obsids": [],
            }),
        ]

        with self.assertLogs("askap_mwa_trigger", level="ERROR") as captured:
            self.worker.process_task(make_task())

        self.assertEqual(mock_get.call_count, 5)
        self.assertIn("/triggervcs", urlsplit(mock_get.call_args_list[3].args[0]).path)
        self.assertTrue(
            any("protected scheduled observation" in line for line in captured.output)
        )

    @patch("requests.Session.get")
    def test_http_200_with_success_false_is_not_treated_as_success(self, mock_get):
        mock_get.side_effect = [
            json_response({
                "success": False,
                "trigger_id": 20,
                "errors": {"0": "buffer unavailable"},
            }),
            json_response({
                "trigger_id": 20,
                "success": False,
                "trigger_mode": "BUFFER",
                "errors": ["buffer unavailable"],
            }),
        ]

        with self.assertLogs("askap_mwa_trigger", level="ERROR") as captured:
            ok = self.worker._call_trigger(
                "triggerbuffer",
                self.worker.build_triggerbuffer_url(make_task()),
                "BUFFER",
                make_task(),
            )

        self.assertFalse(ok)
        self.assertTrue(any("buffer unavailable" in line for line in captured.output))


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
