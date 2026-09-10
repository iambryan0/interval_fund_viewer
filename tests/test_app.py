import contextlib
import io
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import app
import server
import store


class PickPortTest(unittest.TestCase):
    def test_returns_the_preferred_port_when_free(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            free = probe.getsockname()[1]
        self.assertEqual(app.pick_port(free), free)

    def test_moves_on_when_the_preferred_port_is_taken(self):
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen(1)
            port = taken.getsockname()[1]
            self.assertNotEqual(app.pick_port(port), port)

    def test_raises_when_nothing_is_free(self):
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen(1)
            port = taken.getsockname()[1]
            with self.assertRaises(OSError):
                app.pick_port(port, attempts=1)

    def test_the_probe_never_sets_so_reuseaddr(self):
        """On Windows SO_REUSEADDR lets the probe bind a port that already has
        a listener, so it must not set it."""
        with socket.socket() as free_probe:
            free_probe.bind(("127.0.0.1", 0))
            free = free_probe.getsockname()[1]

        options = []
        real_socket = socket.socket

        class RecordingSocket(real_socket):
            def setsockopt(self, level, optname, *args):
                options.append((level, optname))
                return real_socket.setsockopt(self, level, optname, *args)

        socket.socket = RecordingSocket
        try:
            app.pick_port(free)
        finally:
            socket.socket = real_socket

        self.assertNotIn((socket.SOL_SOCKET, socket.SO_REUSEADDR), options)


class FindRunningTest(unittest.TestCase):
    """Second launch finds the copy already serving this database."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "funds.db"
        conn = store.connect(self.db)
        store.init_db(conn)
        conn.close()
        self.httpd = server.make_server(0, self.db)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def test_finds_the_instance_on_the_preferred_port(self):
        self.assertEqual(app.find_running(self.port, self.db), self.port)

    def test_finds_the_instance_further_up_the_port_window(self):
        # the first launch may itself have been bumped by a foreign listener
        self.assertEqual(app.find_running(self.port - 3, self.db, attempts=10),
                         self.port)

    def test_none_when_nothing_is_listening(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            free = probe.getsockname()[1]
        self.assertIsNone(app.find_running(free, self.db, attempts=1))

    def test_none_when_the_port_belongs_to_something_else(self):
        with socket.socket() as other:
            other.bind(("127.0.0.1", 0))
            other.listen(1)
            port = other.getsockname()[1]

            def answer():
                conn, _ = other.accept()
                conn.recv(1024)
                conn.sendall(b"HTTP/1.0 200 OK\r\nContent-Type: text/plain\r\n\r\nnope")
                conn.close()

            threading.Thread(target=answer, daemon=True).start()
            self.assertIsNone(app.find_running(port, self.db, attempts=1))

    def test_none_when_the_instance_serves_a_different_database(self):
        other_db = Path(self.tmp.name) / "other.db"
        self.assertIsNone(app.find_running(self.port, other_db, attempts=1))


class MainReusesRunningInstanceTest(unittest.TestCase):
    """Clicking the shortcut twice opens the running copy, not a second server."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "funds.db"
        self._db_path = store.DB_PATH
        self._csv = store.CSV_PATH
        self._seed = store.SEED_CSV_PATH
        self._csv_error = server.Handler.csv_error
        store.DB_PATH = self.db
        store.CSV_PATH = Path(self.tmp.name) / "redemptions.csv"
        store.SEED_CSV_PATH = Path(self.tmp.name) / "no-such-seed.csv"
        server.Handler.csv_error = ""

        self.httpd = server.make_server(0, self.db)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        app.bootstrap(self.db)
        conn = store.connect(self.db)
        store.set_setting(conn, "port", str(self.port))
        conn.close()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        store.DB_PATH = self._db_path
        store.CSV_PATH = self._csv
        store.SEED_CSV_PATH = self._seed
        server.Handler.csv_error = self._csv_error
        self.tmp.cleanup()

    def test_main_opens_the_browser_at_the_running_instance_and_exits(self):
        opened = []
        started = []
        out = io.StringIO()
        with patch.object(app.webbrowser, "open", opened.append), \
                patch.object(server, "run", lambda *a: started.append(a)), \
                contextlib.redirect_stdout(out):
            code = app.main([])
        self.assertEqual(code, 0)
        self.assertEqual(opened, [f"http://127.0.0.1:{self.port}/"])
        self.assertEqual(started, [])
        self.assertIn("already running", out.getvalue().lower())


class BootstrapTest(unittest.TestCase):
    def setUp(self):
        self._csv_error = server.Handler.csv_error
        server.Handler.csv_error = ""
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "funds.db"
        # keep the seed lookup local so the real seed file never leaks in
        self._seed = store.SEED_CSV_PATH
        store.SEED_CSV_PATH = Path(self.tmp.name) / "no-such-seed.csv"
        # same for the csv bootstrap writes
        self._csv = store.CSV_PATH
        store.CSV_PATH = Path(self.tmp.name) / "redemptions.csv"

    def tearDown(self):
        server.Handler.csv_error = self._csv_error
        store.SEED_CSV_PATH = self._seed
        store.CSV_PATH = self._csv
        self.tmp.cleanup()

    def test_creates_the_schema(self):
        app.bootstrap(self.db)
        conn = store.connect(self.db)
        self.assertEqual(store.list_funds(conn), [])
        conn.close()

    def test_seeds_the_user_agent_from_the_environment(self):
        import os
        os.environ["SEC_USER_AGENT"] = "Env Person env@example.com"
        try:
            app.bootstrap(self.db)
        finally:
            del os.environ["SEC_USER_AGENT"]
        conn = store.connect(self.db)
        self.assertEqual(store.get_setting(conn, "sec_user_agent"),
                         "Env Person env@example.com")
        conn.close()

    def test_does_not_overwrite_a_saved_user_agent(self):
        import os
        app.bootstrap(self.db)
        conn = store.connect(self.db)
        store.set_setting(conn, "sec_user_agent", "Saved saved@example.com")
        conn.close()
        os.environ["SEC_USER_AGENT"] = "Env Person env@example.com"
        try:
            app.bootstrap(self.db)
        finally:
            del os.environ["SEC_USER_AGENT"]
        conn = store.connect(self.db)
        self.assertEqual(store.get_setting(conn, "sec_user_agent"),
                         "Saved saved@example.com")
        conn.close()

    def test_imports_a_legacy_csv_sitting_beside_the_database(self):
        legacy = Path(self.tmp.name) / "redemptions.csv"
        legacy.write_text(
            "cik,ticker,fund_name,next_redemption_date,filing_date,filing_url,"
            "confidence,updated_at\n"
            "1234567,ACME,Acme Interval Fund,2026-09-12,2026-08-14,https://x,0.9,t\n",
            encoding="utf-8",
        )
        app.bootstrap(self.db, legacy_csv=legacy)
        conn = store.connect(self.db)
        self.assertIsNotNone(store.get_fund(conn, "1234567"))
        conn.close()

    def test_bootstrap_seeds_an_empty_db_from_the_seed_beside_it(self):
        # disaster recovery: a fresh db next to redemptions-seed.csv fills
        # itself on launch, and the export is rewritten to match
        export = Path(self.tmp.name) / "redemptions.csv"
        export.write_text("fund_name,ticker,next_redemption_date\n",
                          encoding="utf-8")
        seed = Path(self.tmp.name) / "redemptions-seed.csv"
        seed.write_text(
            "cik,ticker,fund_name,next_redemption_date\n"
            "1234567,\"ACME, ACMX\",Acme Interval Fund,2026-09-12\n",
            encoding="utf-8")

        store.CSV_PATH, store.SEED_CSV_PATH = export, seed
        app.bootstrap(self.db)

        conn = store.connect(self.db)
        try:
            fund = store.get_fund(conn, "1234567")
            self.assertEqual(fund["ticker"], "ACME; ACMX")
            self.assertEqual(fund["next_redemption_date"], "2026-09-12")
        finally:
            conn.close()
        self.assertEqual(export.read_text(encoding="utf-8").splitlines(),
                         ["fund_name,ticker,next_redemption_date",
                          "Acme Interval Fund,ACME; ACMX,2026-09-12"])

    def test_bootstrap_does_not_reseed_a_db_that_has_funds(self):
        seed = Path(self.tmp.name) / "redemptions-seed.csv"
        seed.write_text(
            "cik,ticker,fund_name,next_redemption_date\n"
            "1234567,ACME,Acme Interval Fund,2026-09-12\n", encoding="utf-8")
        store.SEED_CSV_PATH = seed
        app.bootstrap(self.db)
        conn = store.connect(self.db)
        try:
            store.delete_fund(conn, "1234567")
            store.add_fund(conn, "7654321", "OTHR", "Other Fund")
        finally:
            conn.close()

        app.bootstrap(self.db)  # second launch, seed still beside the app
        conn = store.connect(self.db)
        try:
            self.assertEqual([f["cik"] for f in store.list_funds(conn)],
                             ["7654321"])
        finally:
            conn.close()

    def test_bootstrap_writes_the_csv_to_match_the_db(self):
        seed = Path(self.tmp.name) / "no-such-seed.csv"
        seed.write_text("cik,ticker,fund_name,next_redemption_date\n"
                        "1234567,ACME,Acme Interval Fund,2026-09-12\n",
                        encoding="utf-8")
        app.bootstrap(self.db, legacy_csv=seed)
        self.assertEqual(
            store.CSV_PATH.read_bytes(),
            b"fund_name,ticker,next_redemption_date\r\n"
            b"Acme Interval Fund,ACME,2026-09-12\r\n")

    def test_bootstrap_rebuilds_a_deleted_or_edited_csv(self):
        app.bootstrap(self.db)
        store.CSV_PATH.write_text("hand-edited nonsense\n", encoding="utf-8")
        app.bootstrap(self.db)
        self.assertTrue(store.CSV_PATH.read_text(encoding="utf-8")
                        .startswith("fund_name,ticker,next_redemption_date"))

    def test_bootstrap_survives_an_unwritable_csv(self):
        blocked = Path(self.tmp.name) / "blocked.csv"
        blocked.mkdir()
        import contextlib, io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            app.bootstrap(self.db, csv_path=blocked)
        self.assertIn("WARNING", err.getvalue())
        self.assertTrue(server.Handler.csv_error)
        conn = store.connect(self.db)
        try:
            store.list_funds(conn)  # schema exists so bootstrap finished
        finally:
            conn.close()

    def test_successful_bootstrap_clears_an_earlier_export_error(self):
        server.Handler.csv_error = "Earlier failure"
        app.bootstrap(self.db)
        self.assertEqual(server.Handler.csv_error, "")

    def test_bootstrap_is_idempotent(self):
        app.bootstrap(self.db)
        app.bootstrap(self.db)
        conn = store.connect(self.db)
        self.assertEqual(store.list_funds(conn), [])
        conn.close()


if __name__ == "__main__":
    unittest.main()
