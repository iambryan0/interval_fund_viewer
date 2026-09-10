import socket
import tempfile
import unittest
from pathlib import Path

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
