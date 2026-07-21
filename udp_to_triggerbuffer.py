#!/usr/bin/env python3
"""
UDP candidate stream -> MWA buffer dump and all-sky VCS trigger

- UDP receiver thread: recv, parse (8 columns), enqueue
- HTTP worker thread(s): buffer dump, busy check, VCS trigger, and record verification

secure_key is read from environment variable: TRIGGER_SECURE_KEY
export TRIGGER_SECURE_KEY="IAmASecret"

past dump seconds is configurable via --past-seconds
"""

import argparse
import collections
import csv
import json
import logging
import os
import queue
import socket
import struct
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import requests


LOG = logging.getLogger("askap_mwa_trigger")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ENV_FILE = os.path.join(SCRIPT_DIR, ".env")
DEFAULT_TRIGGER_CSV = os.path.join(SCRIPT_DIR, "trigger_records.csv")
TRIGGER_CSV_LOCK = threading.Lock()


# -----------------------------
# Optional: astropy for time conversion (recommended)
# -----------------------------
_HAS_ASTROPY = False
try:
    from astropy.time import Time  # type: ignore

    _HAS_ASTROPY = True
except Exception:
    _HAS_ASTROPY = False


# -----------------------------
# Data model
# -----------------------------
@dataclass(frozen=True)
class UdpTask:
    addr: Tuple[str, int]
    raw_text: str
    # (sn, tfile, time_from_file, ibc, idt, dm, ibeam, cand_mjd)
    fields: Tuple[float, int, float, int, int, float, int, float]


# -----------------------------
# Helpers
# -----------------------------
def is_multicast(ip: str) -> bool:
    first_octet = int(ip.split(".")[0])
    return 224 <= first_octet <= 239


def parse_hostport(hostport: str) -> Tuple[str, int]:
    host, port_s = hostport.split(":")
    return host, int(port_s)


def parse_candidate_8cols(text: str) -> Optional[Tuple[float, int, float, int, int, float, int, float]]:
    """
    Parse one line formatted like:
      "%0.2f %lu %0.4f %d %d %0.2f %d %0.9f\n"
    -> (sn, tfile, time_from_file, ibc, idt, dm, ibeam, cand_mjd)
    """
    columns = text.strip().split()
    if len(columns) != 8:
        return None
    try:
        sn = float(columns[0])
        tfile = int(columns[1])
        time_from_file = float(columns[2])
        ibc = int(columns[3])
        idt = int(columns[4])
        dm = float(columns[5])
        ibeam = int(columns[6])
        cand_mjd = float(columns[7])
    except Exception:
        return None
    return (sn, tfile, time_from_file, ibc, idt, dm, ibeam, cand_mjd)


# Manual fallback if astropy not installed:
# MJD(UTC) -> Unix seconds (UTC): unix = (mjd - 40587) * 86400
# Unix -> GPS seconds: gps = unix - 315964800 + (GPS-UTC)
# GPS-UTC is 18s for recent years, but can change if leap seconds change.
GPS_UTC_LEAP_SECONDS_FALLBACK = 18
UNIX_GPS_EPOCH = 315964800
MJD_UNIX_EPOCH = 40587.0


def mjd_to_gps_seconds(mjd: float) -> int:
    """
    Convert MJD (assumed UTC) to GPS seconds.
    Prefer astropy (handles leap seconds properly), fallback to constant offset.
    """
    if _HAS_ASTROPY:
        # astropy Time.gps returns GPS seconds (float)
        return int(round(Time(mjd, format="mjd", scale="utc").gps))

    unix_utc = (mjd - MJD_UNIX_EPOCH) * 86400.0
    gps = unix_utc - UNIX_GPS_EPOCH + GPS_UTC_LEAP_SECONDS_FALLBACK
    return int(round(gps))


def load_env_file(path: str) -> None:
    """Load simple KEY=VALUE entries without overwriting exported variables."""
    env_path = os.path.expanduser(path)
    if not os.path.exists(env_path):
        return

    with open(env_path, encoding="utf-8") as env_file:
        for line_number, raw_line in enumerate(env_file, 1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            if "=" not in line:
                raise RuntimeError(
                    f"Invalid environment entry at {env_path}:{line_number}"
                )
            name, value = line.split("=", 1)
            name = name.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1]:
                if value[0] in ("'", '"'):
                    value = value[1:-1]
            if not name:
                raise RuntimeError(
                    f"Empty environment variable name at {env_path}:{line_number}"
                )
            os.environ.setdefault(name, value)


def get_secure_key_from_env(env_name: str = "TRIGGER_SECURE_KEY") -> str:
    key = os.environ.get(env_name, "")
    if not key:
        raise RuntimeError(f"Environment variable {env_name} is not set")
    return key


# -----------------------------
# UDP receiver thread
# -----------------------------
class UDPReceiver(threading.Thread):
    """
    Receive UDP packets, parse, enqueue.
    Never do HTTP here.
    """
    def __init__(
        self,
        host: str,
        port: int,
        out_queue: "queue.Queue[UdpTask]",
        stop_event: threading.Event,
        *,
        recv_bufsize: int = 4096,
        reuse_addr: bool = True,
        join_multicast: bool = True,
        verbose: bool = False,
        drop_when_full: bool = True,
    ):
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self.out_queue = out_queue
        self.stop_event = stop_event
        self.recv_bufsize = recv_bufsize
        self.reuse_addr = reuse_addr
        self.join_multicast = join_multicast
        self.verbose = verbose
        self.drop_when_full = drop_when_full
        self.sock: Optional[socket.socket] = None

    def open_socket(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if self.reuse_addr:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        multicast = self.join_multicast and is_multicast(self.host)
        sock.bind(("" if multicast else self.host, self.port))

        if multicast:
            print(f"[udp] Joining multicast group {self.host}")
            mreq = struct.pack("=4s4s", socket.inet_aton(self.host), socket.inet_aton("0.0.0.0"))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

        # timeout so we can check stop_event and exit cleanly
        sock.settimeout(1.0)

        self.sock = sock
        print(f"[udp] Listening on {self.host}:{self.port}")

    def close_socket(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def run(self) -> None:
        self.open_socket()
        try:
            while not self.stop_event.is_set():
                try:
                    data, addr = self.sock.recvfrom(self.recv_bufsize)  # type: ignore[union-attr]
                except socket.timeout:
                    continue
                except OSError:
                    break

                text = data.decode("utf-8", errors="ignore").strip()
                fields = parse_candidate_8cols(text)
                if fields is None:
                    if self.verbose:
                        print(f"[udp] Unparsed from {addr}: {text!r}")
                    continue

                task = UdpTask(addr=addr, raw_text=text, fields=fields)

                try:
                    if self.drop_when_full:
                        self.out_queue.put_nowait(task)
                    else:
                        self.out_queue.put(task, timeout=0.5)
                except queue.Full:
                    if self.verbose:
                        print("[udp] Queue full, dropping packet.")
        finally:
            self.close_socket()
            print("[udp] Receiver stopped.")


# -----------------------------
# HTTP workflow: TriggerBuffer -> Busy -> TriggerVCS -> Find
# -----------------------------
class TriggerBufferWorker(threading.Thread):
    """Execute the complete MWA rapid-response workflow for each candidate."""

    def __init__(
        self,
        name: str,
        in_queue: "queue.Queue[UdpTask]",
        stop_event: threading.Event,
        *,
        endpoint: str,
        project_id: str,
        secure_key_env: str,
        past_seconds: int,
        obstime: int,
        pretty: bool,
        pretend: bool,
        use_start_time_zero: bool,
        min_trigger_interval_sec: float,
        min_sn: float = 20.0,
        min_dm: float = 0.0,
        burst_window_sec: float = 1.0,
        burst_max_count: int = 10,
        timeout: Tuple[float, float] = (2.0, 10.0),
        max_retries: int = 2,
        retry_backoff_sec: float = 0.5,
        verbose: bool = False,
        debug_url: Optional[str] = None,
        busy_endpoint: Optional[str] = None,
        triggervcs_endpoint: Optional[str] = None,
        show_endpoint: Optional[str] = None,
        creator: str = "askap-mwa-frb-trigger",
        obsname: str = "ASKAP_FRB",
        verify_attempts: int = 3,
        verify_delay_sec: float = 1.0,
        show_trigger_url: bool = False,
        trigger_csv: Optional[str] = None,
    ):
        super().__init__(daemon=True, name=name)
        self.in_queue = in_queue
        self.stop_event = stop_event
        self.endpoint = endpoint.rstrip("?")
        self.busy_endpoint = busy_endpoint or self._sibling_endpoint(endpoint, "busy")
        self.triggervcs_endpoint = triggervcs_endpoint or self._sibling_endpoint(endpoint, "triggervcs")
        self.show_endpoint = show_endpoint or self._sibling_endpoint(endpoint, "show")
        self.project_id = project_id
        self.secure_key_env = secure_key_env
        self.past_seconds = int(past_seconds)
        self.obstime = int(obstime)
        self.pretty = bool(pretty)
        self.pretend = bool(pretend)
        self.use_start_time_zero = bool(use_start_time_zero)
        self.min_trigger_interval_sec = float(min_trigger_interval_sec)
        self.min_sn = float(min_sn)
        self.min_dm = float(min_dm)
        self.burst_window_sec = float(burst_window_sec)
        self.burst_max_count = int(burst_max_count)
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff_sec = retry_backoff_sec
        self.verbose = verbose
        self.debug_url = debug_url.rstrip("?") if debug_url else None
        self.creator = creator
        self.obsname = obsname
        self.verify_attempts = max(1, int(verify_attempts))
        self.verify_delay_sec = max(0.0, float(verify_delay_sec))
        self.show_trigger_url = bool(show_trigger_url)
        self.trigger_csv = os.path.abspath(trigger_csv) if trigger_csv else None

        self.session = requests.Session()
        self._last_http_exchanges: Dict[str, list] = {}
        self._lock = threading.Lock()
        self._last_trigger_monotonic: float = 0.0
        self._burst_timestamps: collections.deque = collections.deque()

    @staticmethod
    def _sibling_endpoint(endpoint: str, service: str) -> str:
        """Replace the final path component with another trigger service."""
        parts = urlsplit(endpoint)
        path = parts.path.rstrip("/")
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        return urlunsplit((parts.scheme, parts.netloc, f"{parent}/{service}", "", ""))

    def _common_trigger_params(self) -> Dict[str, Any]:
        params = {
            "project_id": self.project_id,
            "secure_key": get_secure_key_from_env(self.secure_key_env),
            "pretend": "true" if self.pretend else "false",
        }
        if self.pretty:
            params["pretty"] = "true"
        return params

    def build_triggerbuffer_url(self, task: UdpTask) -> str:
        params = self._common_trigger_params()
        cand_mjd = task.fields[-1]
        cand_gps = mjd_to_gps_seconds(cand_mjd)
        start_time = 0 if self.use_start_time_zero else max(0, cand_gps - self.past_seconds)

        # A finite end_time requests only historical buffered data. TriggerVCS
        # below is responsible for future capture.
        params.update({"start_time": int(start_time), "end_time": int(cand_gps)})
        return f"{self.endpoint}?{urlencode(params)}"

    def build_busy_url(self) -> str:
        params: Dict[str, Any] = {
            "project_id": self.project_id,
            "obstime": int(self.obstime),
        }
        if self.pretty:
            params["pretty"] = "true"
        return f"{self.busy_endpoint}?{urlencode(params)}"

    def build_triggervcs_url(self) -> str:
        params = self._common_trigger_params()
        params.update({
            "creator": self.creator,
            "obsname": self.obsname,
            "exptime": int(self.obstime),
            "nobs": 1,
        })
        # Intentionally omit ra/dec/source/alt/az. The MWA API defines no
        # target position as all-sky mode (one dipole active per tile).
        return f"{self.triggervcs_endpoint}?{urlencode(params)}"

    def build_show_url(self, trigger_id: int) -> str:
        params: Dict[str, Any] = {"trigger_id": trigger_id}
        if self.pretty:
            params["pretty"] = "true"
        return f"{self.show_endpoint}?{urlencode(params)}"

    def send_debug_event(self, task: UdpTask) -> None:
        """Send all parsed fields and sender metadata to the debug URL."""
        if not self.debug_url:
            return
        sn, tfile, time_from_file, ibc, idt, dm, ibeam, cand_mjd = task.fields
        params = {
            "sn": f"{sn:.2f}",
            "tfile": str(tfile),
            "time_from_file": f"{time_from_file:.4f}",
            "ibc": str(ibc),
            "idt": str(idt),
            "dm": f"{dm:.2f}",
            "ibeam": str(ibeam),
            "cand_mjd": f"{cand_mjd:.9f}",
            "sender_ip": task.addr[0],
            "sender_port": str(task.addr[1]),
        }
        try:
            response = self.session.get(
                f"{self.debug_url}?{urlencode(params)}", timeout=self.timeout
            )
            if self.verbose:
                LOG.debug("debug event returned HTTP %s", response.status_code)
        except requests.RequestException as exc:
            LOG.warning("failed to send debug event: %s", exc)

    @staticmethod
    def _safe_request_params(url: str) -> Dict[str, Any]:
        """Return URL parameters suitable for logs, excluding credentials."""
        parsed = parse_qs(urlsplit(url).query, keep_blank_values=True)
        parsed.pop("secure_key", None)
        return {
            key: values[0] if len(values) == 1 else values
            for key, values in parsed.items()
        }

    def _append_trigger_csv(
        self,
        *,
        service: str,
        expected_mode: str,
        requested_utc: str,
        task: UdpTask,
        request_params: Dict[str, Any],
        payload: Optional[Dict[str, Any]],
        history: Dict[str, Any],
        api_exchanges: list,
        busy_exchanges: list,
        filter_status: str = "accepted",
        filter_reason: str = "",
    ) -> None:
        if not self.trigger_csv:
            return

        sn, _, _, _, _, dm, ibeam, cand_mjd = task.fields
        payload = payload or {}
        row = {
            "requested_utc": requested_utc,
            "recorded_utc": datetime.now(timezone.utc).isoformat(),
            "filter_status": filter_status,
            "filter_reason": filter_reason,
            "service": service,
            "expected_mode": expected_mode,
            "sender_ip": task.addr[0],
            "sender_port": task.addr[1],
            "sn": sn,
            "dm": dm,
            "ibeam": ibeam,
            "cand_mjd": f"{cand_mjd:.9f}",
            "project_id": self.project_id,
            "pretend": self.pretend,
            "request_params": json.dumps(request_params, sort_keys=True),
            "trigger_id": payload.get("trigger_id", ""),
            "api_success": payload.get("success", ""),
            "api_obsids": json.dumps(
                payload.get("obsid_list", payload.get("obsids", [])),
                default=str,
            ),
            "api_raw_responses": json.dumps(api_exchanges, default=str),
            "api_errors": (
                "" if service == "candidate_filter"
                else "" if payload.get("success") is True
                else self._errors(payload) if payload
                else "invalid response"
            ),
            "history_found": history.get("found", False),
            "history_id": history.get("id", ""),
            "history_mode": history.get("trigger_mode", ""),
            "history_success": history.get("success", ""),
            "history_obsids": json.dumps(history.get("obsids", []), default=str),
            "history_errors": history.get("errors", ""),
            "history_created_datetime": history.get("created_datetime", ""),
            "history_raw_responses": json.dumps(
                history.get("raw_responses", []), default=str
            ),
            "busy_raw_responses": json.dumps(busy_exchanges, default=str),
        }
        fieldnames = list(row.keys())

        try:
            os.makedirs(os.path.dirname(self.trigger_csv), exist_ok=True)
            with TRIGGER_CSV_LOCK:
                write_header = (
                    not os.path.exists(self.trigger_csv)
                    or os.path.getsize(self.trigger_csv) == 0
                )
                if not write_header:
                    with open(
                        self.trigger_csv, newline="", encoding="utf-8"
                    ) as existing_file:
                        existing_header = next(csv.reader(existing_file), [])
                    if existing_header != fieldnames:
                        suffix = datetime.now(timezone.utc).strftime(
                            "%Y%m%dT%H%M%SZ"
                        )
                        backup_path = f"{self.trigger_csv}.{suffix}.bak"
                        os.replace(self.trigger_csv, backup_path)
                        os.chmod(backup_path, 0o600)
                        LOG.warning(
                            "trigger CSV schema changed; previous file moved to %s",
                            backup_path,
                        )
                        write_header = True

                with open(self.trigger_csv, "a", newline="", encoding="utf-8") as csv_file:
                    writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
                    if write_header:
                        writer.writeheader()
                    writer.writerow(row)
                os.chmod(self.trigger_csv, 0o600)
            LOG.info(
                "trigger CSV recorded service=%s trigger_id=%s file=%s",
                service, row["trigger_id"], self.trigger_csv,
            )
        except OSError as exc:
            LOG.error("failed to write trigger CSV %s: %s", self.trigger_csv, exc)

    def _append_filtered_candidate_csv(self, task: UdpTask, reason: str) -> None:
        """Record a parsed candidate rejected by a local filter."""
        self._append_trigger_csv(
            service="candidate_filter",
            expected_mode="",
            requested_utc=datetime.now(timezone.utc).isoformat(),
            task=task,
            request_params={
                "min_sn": self.min_sn,
                "min_dm": self.min_dm,
                "burst_window_sec": self.burst_window_sec,
                "burst_max_count": self.burst_max_count,
                "min_trigger_interval_sec": self.min_trigger_interval_sec,
            },
            payload=None,
            history={},
            api_exchanges=[],
            busy_exchanges=[],
            filter_status="rejected",
            filter_reason=reason,
        )

    @staticmethod
    def _payload(data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get("result"), dict):
            return data["result"]
        if isinstance(data, list) and len(data) == 1:
            return data[0]
        return data

    @staticmethod
    def _errors(payload: Any) -> str:
        if not isinstance(payload, dict):
            return "response is not a JSON object"
        errors = payload.get("errors")
        if isinstance(errors, dict):
            return "; ".join(str(value) for value in errors.values()) or "unspecified error"
        if isinstance(errors, list):
            return "; ".join(str(value) for value in errors) or "unspecified error"
        return str(errors or "unspecified error")

    def _request_json(
        self, label: str, url: str, *, retries: Optional[int] = None
    ) -> Optional[Any]:
        """GET an API endpoint and retain every raw HTTP response attempt."""
        endpoint = url.split("?", 1)[0]  # never store secure_key in request metadata
        retry_count = self.max_retries if retries is None else max(0, retries)
        exchanges = []
        self._last_http_exchanges[label] = exchanges

        for attempt in range(retry_count + 1):
            try:
                response = self.session.get(url, timeout=self.timeout)
                exchanges.append({
                    "attempt": attempt + 1,
                    "received_utc": datetime.now(timezone.utc).isoformat(),
                    "endpoint": endpoint,
                    "http_status": response.status_code,
                    "raw_response": response.text,
                    "error": "",
                })
                if not 200 <= response.status_code < 300:
                    raise RuntimeError(f"HTTP {response.status_code}")
                try:
                    data = response.json()
                except ValueError as exc:
                    raise RuntimeError("response was not valid JSON") from exc
                if self.verbose:
                    LOG.debug(
                        "%s returned HTTP %s from %s",
                        label, response.status_code, endpoint,
                    )
                return data
            except requests.RequestException as exc:
                exchanges.append({
                    "attempt": attempt + 1,
                    "received_utc": datetime.now(timezone.utc).isoformat(),
                    "endpoint": endpoint,
                    "http_status": "",
                    "raw_response": "",
                    "error": repr(exc),
                })
                failure = exc
            except RuntimeError as exc:
                failure = exc

            if attempt >= retry_count:
                LOG.error("%s request failed at %s: %s", label, endpoint, failure)
                return None
            time.sleep(self.retry_backoff_sec * (2 ** attempt))
        return None

    def _verify_trigger_record(
        self, trigger_id: int, expected_mode: str, expected_success: bool,
        audit: Optional[Dict[str, Any]] = None,
    ) -> bool:
        history_exchanges = []
        for attempt in range(self.verify_attempts):
            data = self._request_json("find", self.build_show_url(trigger_id))
            history_exchanges.extend(self._last_http_exchanges.get("find", []))
            if audit is not None:
                audit["raw_responses"] = list(history_exchanges)
            payload = self._payload(data)
            record_id = None
            if isinstance(payload, dict):
                record_id = payload.get("trigger_id", payload.get("id"))
            if isinstance(payload, dict) and record_id is not None:
                try:
                    record_id_matches = int(record_id) == trigger_id
                except (TypeError, ValueError):
                    record_id_matches = False
                if not record_id_matches:
                    LOG.error(
                        "trigger history ID mismatch: expected %s, got %r",
                        trigger_id, record_id,
                    )
                    return False
                record_success = payload.get("success") is True
                record_mode = payload.get("trigger_mode")
                if record_mode and record_mode != expected_mode:
                    LOG.error(
                        "trigger %s record mode mismatch: expected %s, got %s",
                        trigger_id, expected_mode, record_mode,
                    )
                    return False
                if record_success != expected_success:
                    LOG.error(
                        "trigger %s record success=%s; errors=%s",
                        trigger_id, record_success, self._errors(payload),
                    )
                    return False
                history_obsids = payload.get("obsids", payload.get("obsid_list", []))
                history_summary = {
                    "found": True,
                    "id": record_id,
                    "trigger_mode": record_mode,
                    "pretend": payload.get("pretend"),
                    "success": record_success,
                    "obsids": history_obsids,
                    "errors": payload.get("errors"),
                    "creator": payload.get("creator"),
                    "obsname": payload.get("obsname"),
                    "created_datetime": payload.get("created_datetime"),
                }
                if audit is not None:
                    audit.update(history_summary)
                LOG.info(
                    "trigger history record=%s",
                    json.dumps(history_summary, sort_keys=True, default=str),
                )
                if record_success:
                    LOG.info(
                        "verified %s trigger %s in trigger history (obsids=%s)",
                        expected_mode, trigger_id, history_obsids,
                    )
                else:
                    LOG.error(
                        "verified failed %s trigger %s; reason: %s",
                        expected_mode, trigger_id, self._errors(payload),
                    )
                return True

            if attempt + 1 < self.verify_attempts:
                time.sleep(self.verify_delay_sec)
        LOG.error("trigger %s was not found in trigger history", trigger_id)
        return False

    def _call_trigger(
        self, label: str, url: str, expected_mode: str, task: UdpTask,
        busy_exchanges: Optional[list] = None,
    ) -> bool:
        requested_utc = datetime.now(timezone.utc).isoformat()
        if self.show_trigger_url:
            print(f"[http] {label} URL: {url}", flush=True)
        safe_params = self._safe_request_params(url)
        LOG.info(
            "trigger request service=%s params=%s",
            label, json.dumps(safe_params, sort_keys=True),
        )

        history: Dict[str, Any] = {}
        # Trigger calls are not idempotent: never retry automatically.
        data = self._request_json(label, url, retries=0)
        api_exchanges = list(self._last_http_exchanges.get(label, []))
        busy_exchanges = list(busy_exchanges or [])
        payload = self._payload(data)
        if not isinstance(payload, dict):
            LOG.error(
                "trigger response service=%s invalid_response=%r", label, data
            )
            self._append_trigger_csv(
                service=label,
                expected_mode=expected_mode,
                requested_utc=requested_utc,
                task=task,
                request_params=safe_params,
                payload=None,
                history=history,
                api_exchanges=api_exchanges,
                busy_exchanges=busy_exchanges,
            )
            return False

        success = payload.get("success") is True
        trigger_id = payload.get("trigger_id")
        response_obsids = payload.get("obsid_list", payload.get("obsids", []))
        response_errors = self._errors(payload) if not success else ""
        LOG.info(
            "trigger response service=%s trigger_id=%s success=%s obsids=%s errors=%s",
            label, trigger_id, success, response_obsids, response_errors,
        )
        if not success:
            LOG.error("%s rejected; reason: %s", label, response_errors)

        if trigger_id is None:
            LOG.error("%s response has no trigger_id; history cannot be verified", label)
            self._append_trigger_csv(
                service=label,
                expected_mode=expected_mode,
                requested_utc=requested_utc,
                task=task,
                request_params=safe_params,
                payload=payload,
                history=history,
                api_exchanges=api_exchanges,
                busy_exchanges=busy_exchanges,
            )
            return False
        try:
            trigger_id = int(trigger_id)
        except (TypeError, ValueError):
            LOG.error("%s returned invalid trigger_id=%r", label, trigger_id)
            self._append_trigger_csv(
                service=label,
                expected_mode=expected_mode,
                requested_utc=requested_utc,
                task=task,
                request_params=safe_params,
                payload=payload,
                history=history,
                api_exchanges=api_exchanges,
                busy_exchanges=busy_exchanges,
            )
            return False

        record_matches = self._verify_trigger_record(
            trigger_id, expected_mode, success, audit=history
        )
        self._append_trigger_csv(
            service=label,
            expected_mode=expected_mode,
            requested_utc=requested_utc,
            task=task,
            request_params=safe_params,
            payload=payload,
            history=history,
            api_exchanges=api_exchanges,
            busy_exchanges=busy_exchanges,
        )
        return success and record_matches

    def _check_busy(self) -> Optional[bool]:
        data = self._request_json("busy", self.build_busy_url())
        payload = self._payload(data)
        if isinstance(payload, bool):
            return payload
        if isinstance(payload, dict) and isinstance(payload.get("busy"), bool):
            return payload["busy"]
        LOG.error("busy returned an invalid response: %r", data)
        return None

    def _allowed_to_trigger_now(self) -> bool:
        if self.min_trigger_interval_sec <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            if now - self._last_trigger_monotonic >= self.min_trigger_interval_sec:
                self._last_trigger_monotonic = now
                return True
            return False

    def _is_burst(self) -> bool:
        if self.burst_max_count <= 0:
            return False
        now = time.monotonic()
        with self._lock:
            self._burst_timestamps.append(now)
            cutoff = now - self.burst_window_sec
            while self._burst_timestamps and self._burst_timestamps[0] < cutoff:
                self._burst_timestamps.popleft()
            return len(self._burst_timestamps) > self.burst_max_count

    def process_task(self, task: UdpTask) -> None:
        """Run filters and the ordered MWA workflow for one UDP candidate."""
        self.send_debug_event(task)
        sn, _, _, _, _, dm, ibeam, cand_mjd = task.fields

        if sn < self.min_sn:
            reason = f"sn_below_threshold: {sn:.2f} < {self.min_sn:.2f}"
            self._append_filtered_candidate_csv(task, reason)
            if self.verbose:
                LOG.debug("candidate skipped: SNR %.2f < %.2f", sn, self.min_sn)
            return
        if dm < self.min_dm:
            reason = f"dm_below_threshold: {dm:.2f} < {self.min_dm:.2f}"
            self._append_filtered_candidate_csv(task, reason)
            if self.verbose:
                LOG.debug("candidate skipped: DM %.2f < %.2f", dm, self.min_dm)
            return
        if self._is_burst():
            reason = (
                "burst_safeguard: candidate count exceeded "
                f"{self.burst_max_count} within {self.burst_window_sec:g}s"
            )
            self._append_filtered_candidate_csv(task, reason)
            if self.verbose:
                LOG.debug("candidate skipped by burst safeguard")
            return
        if not self._allowed_to_trigger_now():
            reason = (
                "trigger_rate_limit: minimum interval is "
                f"{self.min_trigger_interval_sec:g}s"
            )
            self._append_filtered_candidate_csv(task, reason)
            if self.verbose:
                LOG.debug("candidate skipped by trigger rate limit")
            return

        LOG.info(
            "candidate accepted sender=%s:%s sn=%.2f dm=%.2f ibeam=%s cand_mjd=%.9f",
            task.addr[0], task.addr[1], sn, dm, ibeam, cand_mjd,
        )

        buffer_ok = self._call_trigger(
            "triggerbuffer", self.build_triggerbuffer_url(task), "BUFFER", task
        )
        if not buffer_ok:
            LOG.error("historical buffer capture was not confirmed")

        busy = self._check_busy()
        if busy is True:
            LOG.warning(
                "MWA reports busy for project %s over the next %ss; "
                "triggervcs will still be attempted",
                self.project_id, self.obstime,
            )
        elif busy is False:
            LOG.info("MWA reports interruptible for project %s", self.project_id)
        else:
            LOG.error("MWA interruptibility could not be determined; triggervcs will still be attempted")

        vcs_ok = self._call_trigger(
            "triggervcs", self.build_triggervcs_url(), "MWAX_VCS", task,
            busy_exchanges=list(self._last_http_exchanges.get("busy", [])),
        )
        if not vcs_ok:
            state = "busy" if busy is True else "not busy" if busy is False else "unknown"
            LOG.error("all-sky VCS trigger failed (pre-check state: %s)", state)
        elif busy is True:
            LOG.warning("all-sky VCS trigger succeeded although the earlier busy check returned true")

    def run(self) -> None:
        if not _HAS_ASTROPY:
            LOG.warning(
                "astropy unavailable; using a fixed GPS-UTC offset for MJD conversion"
            )

        LOG.info("HTTP worker %s started", self.name)
        try:
            while True:
                if self.stop_event.is_set() and self.in_queue.empty():
                    break
                try:
                    task = self.in_queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                try:
                    self.process_task(task)
                except Exception:
                    LOG.exception("unhandled error while processing candidate from %s", task.addr)
                finally:
                    self.in_queue.task_done()
        finally:
            self.session.close()
            LOG.info("HTTP worker %s stopped", self.name)


# -----------------------------
# Main
# -----------------------------
def main():
    bootstrap = argparse.ArgumentParser(add_help=False)
    bootstrap.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    bootstrap_args, _ = bootstrap.parse_known_args()
    load_env_file(bootstrap_args.env_file)

    ap = argparse.ArgumentParser()
    ap.add_argument("hostport", help="UDP bind host:port (e.g. 0.0.0.0:4900 or 224.1.1.1:4900)")
    ap.add_argument("--env-file", default=bootstrap_args.env_file,
                    help="KEY=VALUE file loaded before other defaults (default: .env)")

    ap.add_argument("--endpoint", default="http://mro.mwa128t.org/trigger/triggerbuffer",
                    help="TriggerBuffer endpoint URL")
    ap.add_argument("--busy-endpoint", default=None,
                    help="Busy endpoint URL (default: sibling of --endpoint)")
    ap.add_argument("--triggervcs-endpoint", default=None,
                    help="TriggerVCS endpoint URL (default: sibling of --endpoint)")
    ap.add_argument("--show-endpoint", default="https://ws.mwatelescope.org/trigger/find",
                    help="Trigger history lookup endpoint URL (Find recommended)")

    project_id_default = os.environ.get(
        "PROJECT_ID", os.environ.get("Project_ID", "C001")
    )
    ap.add_argument("--project-id", default=project_id_default,
                    help="MWA project_id (default: PROJECT_ID/Project_ID from env, else C001)")
    ap.add_argument("--secure-key-env", default="TRIGGER_SECURE_KEY",
                    help="Environment variable name holding secure_key")

    ap.add_argument("--past-seconds", type=int, default=120,
                    help="Seconds before candidate time to start buffer dump (used when --use-start-zero is false)")
    ap.add_argument("--obstime", type=int, default=600,
                    help="All-sky VCS duration and Busy look-ahead in seconds (default: 600)")
    ap.add_argument("--creator", default="askap-mwa-frb-trigger",
                    help="Creator recorded for TriggerVCS")
    ap.add_argument("--obsname", default="ASKAP_FRB",
                    help="Observation name recorded for TriggerVCS")

    ap.add_argument("--pretty", action="store_true", help="pretty=true")
    ap.add_argument("--pretend", action="store_true", help="pretend=true (dry-run behavior on the trigger service)")

    ap.add_argument("--use-start-zero", action="store_true",
                    help="Save all currently available historical buffer data. "
                         "Otherwise start at candidate_gps - past_seconds")

    ap.add_argument("--min-sn", type=float, default=20.0,
                    help="Minimum signal-to-noise ratio to forward a trigger (default: 20.0)")
    ap.add_argument("--min-dm", type=float, default=0.0,
                    help="Minimum dispersion measure (pc/cm^3) to forward a trigger (default: 0.0)")
    ap.add_argument("--burst-window", type=float, default=1.0,
                    help="Time window in seconds for burst detection (default: 1.0)")
    ap.add_argument("--burst-max-count", type=int, default=10,
                    help="Max candidates allowed within --burst-window before suppressing trigger (default: 10)")

    ap.add_argument("--min-trigger-interval", type=float, default=2.0,
                    help="Minimum seconds between triggers to avoid spamming (0 disables). Default 2s")

    ap.add_argument("--workers", type=int, default=1, help="Number of HTTP worker threads")
    ap.add_argument("--queue-size", type=int, default=1000, help="Max queued UDP tasks")

    ap.add_argument("--timeout-connect", type=float, default=2.0)
    ap.add_argument("--timeout-read", type=float, default=10.0)
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--verify-attempts", type=int, default=3,
                    help="Attempts to find each trigger in history (default: 3)")
    ap.add_argument("--verify-delay", type=float, default=1.0,
                    help="Seconds between trigger-history checks (default: 1.0)")

    ap.add_argument("--no-drop", action="store_true",
                    help="If set, block briefly when queue is full instead of dropping")

    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--show-trigger-url", action="store_true",
                    help="Print full TriggerBuffer/TriggerVCS URLs (includes secure_key)")
    ap.add_argument("--trigger-csv", default=DEFAULT_TRIGGER_CSV,
                    help="CSV audit file for filtered candidates and Buffer/VCS trigger records")
    ap.add_argument("--debug-url", default=None,
                    help="If set, send ALL parsed UDP fields to this URL for every received candidate (debug mode)")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    LOG.setLevel(logging.DEBUG if args.verbose else logging.INFO)

    if args.past_seconds < 0 or args.obstime <= 0:
        ap.error("--past-seconds must be >= 0 and --obstime must be > 0")

    # Validate secure key exists early (fail fast)
    _ = get_secure_key_from_env(args.secure_key_env)

    host, port = parse_hostport(args.hostport)

    q: "queue.Queue[UdpTask]" = queue.Queue(maxsize=args.queue_size)
    stop_event = threading.Event()

    receiver = UDPReceiver(
        host, port, q, stop_event,
        verbose=args.verbose,
        drop_when_full=not args.no_drop,
    )

    workers = [
        TriggerBufferWorker(
            name=f"w{i+1}",
            in_queue=q,
            stop_event=stop_event,
            endpoint=args.endpoint,
            project_id=args.project_id,
            secure_key_env=args.secure_key_env,
            past_seconds=args.past_seconds,
            obstime=args.obstime,
            pretty=args.pretty,
            pretend=args.pretend,
            use_start_time_zero=args.use_start_zero,
            min_trigger_interval_sec=args.min_trigger_interval,
            min_sn=args.min_sn,
            min_dm=args.min_dm,
            burst_window_sec=args.burst_window,
            burst_max_count=args.burst_max_count,
            timeout=(args.timeout_connect, args.timeout_read),
            max_retries=args.retries,
            verbose=args.verbose,
            debug_url=args.debug_url,
            busy_endpoint=args.busy_endpoint,
            triggervcs_endpoint=args.triggervcs_endpoint,
            show_endpoint=args.show_endpoint,
            creator=args.creator,
            obsname=args.obsname,
            verify_attempts=args.verify_attempts,
            verify_delay_sec=args.verify_delay,
            show_trigger_url=args.show_trigger_url,
            trigger_csv=args.trigger_csv,
        )
        for i in range(args.workers)
    ]

    receiver.start()
    for w in workers:
        w.start()

    print("[main] Running. Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n[main] Ctrl+C received, shutting down...")
        stop_event.set()

        receiver.join(timeout=3.0)
        q.join()
        for w in workers:
            w.join(timeout=3.0)

        print("[main] Done.")


if __name__ == "__main__":
    main()
