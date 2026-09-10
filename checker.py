"""Runs the EDGAR checks on a background thread and reports progress.

One job at a time. If one fund fails I record it and keep going with the
rest.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Callable, Optional

import sec
import store

_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_state: dict = {}


def _empty_state() -> dict:
    return {
        "running": False,
        "total": 0,
        "done": 0,
        "current": "",
        "started_at": None,
        "finished_at": None,
        "errors": [],
        "job_error": "",
    }


_state = _empty_state()


def reset() -> None:
    """Clear job state. Only safe when nothing is running. For tests."""
    global _thread, _state
    _thread = None
    _state = _empty_state()


def is_running() -> bool:
    with _lock:
        return _state["running"]


def progress() -> dict:
    """A copy that's safe to serialise while the job keeps mutating its own."""
    with _lock:
        state = dict(_state)
        state["errors"] = list(_state["errors"])
        return state


def join(timeout: float = 5.0) -> None:
    thread = _thread
    if thread is not None:
        thread.join(timeout)


def start_check(ciks: list[str], user_agent: str, conn_factory: Callable,
                submissions_fn: Optional[Callable] = None,
                detect_fn: Optional[Callable] = None) -> bool:
    """Start a check over `ciks`. Returns False if one is already running."""
    global _thread, _state

    if not ciks:
        return False

    with _lock:
        if _state["running"]:
            return False
        _state = _empty_state()
        _state["running"] = True
        _state["total"] = len(ciks)
        _state["started_at"] = datetime.now(tz=timezone.utc).isoformat(
            timespec="seconds")

    try:
        _thread = threading.Thread(
            target=_run,
            args=(list(ciks), user_agent, conn_factory,
                  submissions_fn or sec.fetch_submissions,
                  detect_fn or sec.detect_latest),
            daemon=True,
        )
        _thread.start()
    except Exception as exc:
        _thread = None  # join() must not try to join an unstarted thread
        _finish(str(exc) or type(exc).__name__)
    return True


def _finish(error: str = "") -> None:
    with _lock:
        _state["job_error"] = error
        _state["running"] = False
        _state["current"] = ""
        _state["finished_at"] = datetime.now(tz=timezone.utc).isoformat(
            timespec="seconds")


def _run(ciks, user_agent, conn_factory, submissions_fn, detect_fn) -> None:
    conn = None
    job_error = ""
    try:
        conn = conn_factory()
        for cik in ciks:
            name = _fund_label(conn, cik)
            with _lock:
                _state["current"] = name
            try:
                submissions = submissions_fn(cik, user_agent)
                filing = detect_fn(submissions)
                store.record_check(conn, cik, filing)
            except Exception as exc:  # noqa: BLE001 - one fund shouldn't stop the run
                message = str(exc) or type(exc).__name__
                store.record_check(conn, cik, None, error=message)
                with _lock:
                    # progress() copies the list but not the dicts in it, so
                    # always append a new one, never edit one in place
                    _state["errors"].append(
                        {"cik": cik, "name": name, "message": message})
            finally:
                with _lock:
                    _state["done"] += 1
    except Exception as exc:
        # Database startup/read/write failures cannot always be saved on a
        # fund row. Keep them in progress so the browser can explain the stop.
        job_error = str(exc) or type(exc).__name__
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception as exc:
            message = str(exc) or type(exc).__name__
            job_error = f"{job_error}; {message}" if job_error else message
        finally:
            _finish(job_error)


def _fund_label(conn, cik: str) -> str:
    fund = store.get_fund(conn, cik)
    if not fund:
        return cik
    return fund["fund_name"] or fund["ticker"] or cik
