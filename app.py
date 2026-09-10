#!/usr/bin/env python3
"""Entry point for the interval-fund review GUI.

    python app.py

Starts a local web server on 127.0.0.1 and opens it in the browser. Standard
library only — nothing to install.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import urllib.request
import webbrowser
from pathlib import Path

import server
import store

DEFAULT_PORT = 8765


def pick_port(preferred: int, attempts: int = 10) -> int:
    """The first free loopback port at or after `preferred`.

    The probe deliberately does not set SO_REUSEADDR: on Windows that option
    permits binding a port that already has a listener, so the probe would
    report a busy port as free and two instances would split incoming
    connections. Without it, a port in TIME_WAIT is skipped instead, and
    main() already reports the port it settled on.
    """
    last_error: OSError | None = None
    for offset in range(attempts):
        candidate = preferred + offset
        try:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", candidate))
            return candidate
        except OSError as exc:
            last_error = exc
    raise OSError(
        f"No free port between {preferred} and {preferred + attempts - 1}."
    ) from last_error


def find_running(preferred: int, db_path, attempts: int = 10,
                 timeout: float = 1.0) -> int | None:
    """Port of an instance already serving `db_path`, or None.

    Clicking the shortcut twice used to start a second server on the next
    port, with its own check job and progress state over the same database.
    Every port pick_port could have settled on is asked /api/health; only a
    reply naming this app and this database counts, so a foreign listener on
    the preferred port or a copy run from another folder is left alone.
    """
    wanted = str(Path(db_path).resolve())
    for offset in range(attempts):
        port = preferred + offset
        url = f"http://127.0.0.1:{port}/api/health"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception:  # noqa: BLE001 - refused, timed out, not json: not us
            continue
        if (isinstance(payload, dict) and payload.get("app") == server.APP_ID
                and payload.get("db") == wanted):
            return port
    return None


def bootstrap(db_path, legacy_csv=None, csv_path=None) -> None:
    """Create the schema, seed the user agent, import the seed csv if the db
    is empty, then write redemptions.csv so it matches the db from the start.

    If the csv can't be written, warn in the console and browser. The db is
    fine; the server still runs and the next change or fund-list reload retries.
    """
    conn = store.connect(db_path)
    try:
        store.init_db(conn)
        if not store.get_setting(conn, "sec_user_agent"):
            from_env = os.environ.get("SEC_USER_AGENT", "").strip()
            if from_env:
                store.set_setting(conn, "sec_user_agent", from_env)
        # Disaster recovery: a fresh db beside redemptions-seed.csv fills
        # itself. The import only runs when the funds table is empty.
        legacy = Path(legacy_csv) if legacy_csv else store.SEED_CSV_PATH
        store.import_legacy_csv(conn, legacy)
        csv = Path(csv_path) if csv_path else store.CSV_PATH
        try:
            store.write_csv(conn, csv)
        except OSError as exc:
            server.Handler.csv_error = f"{exc.strerror or exc}."
            print(f"WARNING: could not write {csv}: {exc}", file=sys.stderr)
        else:
            server.Handler.csv_error = ""
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    db_path = store.DB_PATH
    bootstrap(db_path)

    conn = store.connect(db_path)
    try:
        preferred = store.get_setting(conn, "port", str(DEFAULT_PORT))
    finally:
        conn.close()
    preferred_port = int(preferred) if preferred.isdigit() else DEFAULT_PORT

    running = find_running(preferred_port, db_path)
    if running is not None:
        url = f"http://127.0.0.1:{running}/"
        print(f"Interval fund review is already running at {url}")
        print("Opening it in the browser. Close this window.")
        webbrowser.open(url)
        return 0

    try:
        port = pick_port(preferred_port)
    except OSError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    url = f"http://127.0.0.1:{port}/"
    if port != preferred_port:
        print(f"Port {preferred_port} was busy; using {port}.")
    print(f"Interval fund review running at {url}")
    print("Press Ctrl+C to stop.")

    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    server.run(port, db_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
