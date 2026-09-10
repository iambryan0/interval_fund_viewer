"""Ticker maintenance is offline and must not change filing review state."""
import contextlib
import csv
import io
import json
from pathlib import Path
import sqlite3
import tempfile
from unittest.mock import patch

import sec
import store
from tests.test_server import ServerTestCase
from tests.test_store import FILING, StoreTestCase


class TickerStoreTest(StoreTestCase):
    def setUp(self):
        super().setUp()
        store.add_fund(self.conn, '1', '', 'Example Fund')

    def change(self, intent, **fields):
        revision = store.get_fund(self.conn, '1')['ticker_revision']
        store.change_ticker(self.conn, '1', revision, intent, **fields)
        return store.get_fund(self.conn, '1')

    def test_multiple_tickers_and_csv(self):
        self.change('add', ticker=' exaix ')
        fund = self.change('add', ticker='exaax')
        self.assertEqual(fund['ticker'], 'EXAAX; EXAIX')
        rows = list(csv.DictReader(io.StringIO(store.render_csv(self.conn).decode())))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['ticker'], 'EXAAX; EXAIX')
        self.assertEqual(store.local_ticker_ciks(self.conn, 'exaix'), ['1'])

    def test_remove_restore_preserves_id_and_history(self):
        row = self.change('add', ticker='EXAIX')['tickers'][0]
        self.assertEqual(self.change('remove', ticker_id=row['id'])['ticker'], '')
        self.assertEqual(store.local_ticker_ciks(self.conn, 'EXAIX'), [])
        store.init_db(self.conn)
        self.assertEqual(store.get_fund(self.conn, '1')['ticker'], '')
        restored = self.change('add', ticker='EXAIX')['tickers']
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0]['id'], row['id'])
        history = store.list_ticker_changes(self.conn, '1')
        self.assertEqual(len(history), 3)
        self.assertEqual(json.loads(history[1]['before_json']), ['EXAIX'])
        self.assertEqual(json.loads(history[1]['after_json']), [])

    def test_invalid_and_duplicate_adds_leave_revision_unchanged(self):
        for symbol in ('', '=BAD', 'N/A', 'two symbols'):
            with self.subTest(symbol=symbol), self.assertRaises(store.StoreError):
                self.change('add', ticker=symbol)
        self.assertEqual(store.get_fund(self.conn, '1')['ticker_revision'], 0)
        before = self.change('add', ticker='EXAIX')
        with self.assertRaises(store.StoreError):
            self.change('add', ticker='exaix')
        self.assertEqual(store.get_fund(self.conn, '1'), before)

    def test_cross_fund_symbols_require_confirmation(self):
        store.add_fund(self.conn, '2', 'EXAIX', 'Other Fund')
        with self.assertRaises(store.SharedTickerError):
            self.change('add', ticker='EXAIX')
        self.assertEqual(self.change('add', ticker='EXAIX', allow_shared=True)['ticker'], 'EXAIX')

    def test_stale_revision_cannot_overwrite_new_save(self):
        before = self.change('add', ticker='EXAIX')
        with self.assertRaises(store.TickerConflict):
            store.change_ticker(self.conn, '1', 0, 'add', ticker='EXAAX')
        self.assertEqual(store.get_fund(self.conn, '1'), before)

    def test_foreign_id_cannot_be_removed(self):
        store.add_fund(self.conn, '2', 'OTHER', 'Other Fund')
        other = store.get_fund(self.conn, '2')['tickers'][0]
        with self.assertRaises(store.StoreError):
            self.change('remove', ticker_id=other['id'])
        self.assertEqual(store.get_fund(self.conn, '2')['ticker'], 'OTHER')

    def test_audit_failure_rolls_back_everything(self):
        before = store.get_fund(self.conn, '1')
        self.conn.execute("""CREATE TRIGGER fail_tickers BEFORE INSERT ON ticker_changes
                          BEGIN SELECT RAISE(ABORT, 'audit failed'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.change('add', ticker='EXAIX')
        self.assertEqual(store.get_fund(self.conn, '1'), before)

    def test_ticker_changes_do_not_modify_reviews(self):
        store.record_check(self.conn, '1', FILING)
        store.record_review(self.conn, '1', 'reviewed', '2026-12-18', 'Deadline note', FILING['accession_number'])
        history = store.list_reviews(self.conn, '1')
        self.change('add', ticker='EXAIX')
        store.record_check(self.conn, '1', None, error='HTTP 403')
        fund = store.get_fund(self.conn, '1')
        self.assertEqual(fund['ticker'], 'EXAIX')
        self.assertEqual(fund['next_redemption_date'], '2026-12-18')
        self.assertEqual(fund['note'], 'Deadline note')
        self.assertEqual(store.list_reviews(self.conn, '1'), history)

    def test_delete_fund_cascades(self):
        self.change('add', ticker='EXAIX')
        store.delete_fund(self.conn, '1')
        self.assertEqual(store.list_tickers(self.conn, '1'), [])
        self.assertEqual(store.list_ticker_changes(self.conn, '1'), [])

    def test_seed_explicitly_imports_multiple_tickers(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.closing(store.connect(':memory:')) as conn:
            seed = Path(tmp) / 'seed.csv'
            seed.write_text('cik,ticker,fund_name\n1,AAA; BBB,Fund One\n')
            store.init_db(conn)
            self.assertEqual(store.import_legacy_csv(conn, seed), 1)
            self.assertEqual(store.get_fund(conn, '1')['ticker'], 'AAA; BBB')


class TickerRouteTest(ServerTestCase):
    def setUp(self):
        super().setUp()
        with contextlib.closing(self.conn()) as conn:
            store.add_fund(conn, '1', '', 'Example Fund')

    def change(self, intent, cik='1', **fields):
        with contextlib.closing(self.conn()) as conn:
            revision = store.get_fund(conn, cik)['ticker_revision']
        data = dict(intent=intent, revision=str(revision))
        data.update(fields)
        return self.post(f'/fund/{cik}/tickers', data)

    def test_offline_add_updates_csv_and_roster_search(self):
        with patch.object(sec, 'sec_get', side_effect=AssertionError('No network')):
            status, _, _ = self.change('add', ticker='exaix')
            self.assertEqual(status, 303)
            self.change('add', ticker='EXAAX')
        self.assertIn('EXAAX; EXAIX', store.CSV_PATH.read_text())
        _, body, _ = self.get('/')
        self.assertIn('exaax; exaix', body)
        self.assertIn('data-ticker-open', body)

    def test_panel_only_shows_simple_controls(self):
        self.change('add', ticker='EXAIX')
        _, body, _ = self.get('/fund/1/tickers')
        self.assertIn('data-ticker-panel', body)
        self.assertIn('New ticker', body)
        self.assertIn('Remove EXAIX', body)
        for field in ('class_name_', 'source_', 'none_confirmed', 'resolve_legacy', 'name="note"'):
            self.assertNotIn(field, body)
        self.assertNotIn('Ticker change history', body)

    def test_remove_and_readd_restores_same_record(self):
        self.change('add', ticker='EXAIX')
        with contextlib.closing(self.conn()) as conn:
            old = store.get_fund(conn, '1')['tickers'][0]
        self.change('remove', id=str(old['id']))
        _, body, _ = self.get('/fund/1/tickers')
        self.assertIn('No tickers yet.', body)
        self.assertNotIn('Remove EXAIX', body)
        self.assertNotIn('EXAIX', store.CSV_PATH.read_text())
        self.change('add', ticker='EXAIX')
        with contextlib.closing(self.conn()) as conn:
            rows = store.get_fund(conn, '1')['tickers']
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['id'], old['id'])
            self.assertTrue(rows[0]['on_roster'])
            self.assertEqual(len(store.list_ticker_changes(conn, '1')), 3)

    def test_invalid_or_duplicate_add_preserves_input(self):
        status, body, _ = self.change('add', ticker='<bad>')
        self.assertEqual(status, 400)
        self.assertIn('value="&lt;bad&gt;"', body)
        self.change('add', ticker='EXAIX')
        status, _, _ = self.change('add', ticker='exaix')
        self.assertEqual(status, 400)

    def test_stale_action_keeps_current_roster(self):
        self.change('add', ticker='EXAIX')
        status, body, _ = self.change('add', ticker='EXAAX', revision='0')
        self.assertEqual(status, 409)
        self.assertIn('value="EXAAX"', body)
        self.assertIn('name="revision" value="1"', body)
        with contextlib.closing(self.conn()) as conn:
            self.assertEqual(store.get_fund(conn, '1')['ticker'], 'EXAIX')

    def test_csv_failure_keeps_saved_change_and_reload_retries(self):
        store.CSV_PATH.mkdir()
        self.change('add', ticker='EXAIX')
        _, body, _ = self.get('/fund/1/tickers')
        self.assertIn('redemptions.csv is out of date', body)
        with contextlib.closing(self.conn()) as conn:
            self.assertEqual(store.get_fund(conn, '1')['ticker'], 'EXAIX')
        store.CSV_PATH.rmdir()
        self.get('/')
        self.assertIn('EXAIX', store.CSV_PATH.read_text())

    def test_cross_fund_add_requires_confirmation_but_remove_does_not(self):
        with contextlib.closing(self.conn()) as conn:
            store.add_fund(conn, '2', 'EXAIX', 'Other Fund')
        status, body, _ = self.change('add', ticker='EXAIX')
        self.assertEqual(status, 400)
        self.assertIn('name="allow_shared"', body)
        status, _, _ = self.change('add', ticker='EXAIX', allow_shared='on')
        self.assertEqual(status, 303)
        self.change('add', ticker='EXAAX', allow_shared='on')
        with contextlib.closing(self.conn()) as conn:
            rows = store.get_fund(conn, '1')['tickers']
        status, _, _ = self.change('remove', id=str(rows[1]['id']))
        self.assertEqual(status, 303)

    def test_foreign_id_cannot_remove_ticker(self):
        with contextlib.closing(self.conn()) as conn:
            store.add_fund(conn, '2', 'EXAIX', 'Other Fund')
            key = store.get_fund(conn, '2')['tickers'][0]['id']
        status, _, _ = self.change('remove', id=str(key))
        self.assertEqual(status, 400)
        with contextlib.closing(self.conn()) as conn:
            self.assertEqual(store.get_fund(conn, '2')['ticker'], 'EXAIX')

    def test_new_fund_without_ticker_can_add_one(self):
        with contextlib.closing(self.conn()) as conn:
            store.set_setting(conn, 'sec_user_agent', 'Tester test@example.com')
        with patch.object(sec, 'resolve_query', return_value='2'), patch.object(
                sec, 'fetch_submissions', return_value={'name': 'New Fund', 'tickers': []}):
            status, _, response = self.post('/funds', {'query': '2'})
        self.assertEqual(status, 303)
        self.assertIn('open_tickers=2', response.headers['Location'])
        self.change('add', cik='2', ticker='EXAIX')
        with contextlib.closing(self.conn()) as conn:
            self.assertEqual(store.get_fund(conn, '2')['ticker'], 'EXAIX')

    def test_existing_ticker_redirects_offline(self):
        self.change('add', ticker='EXAIX')
        with patch.object(sec, 'resolve_query', side_effect=AssertionError('No lookup')):
            _, _, response = self.post('/funds', {'query': 'exaix'})
        self.assertTrue(response.headers['Location'].startswith('/?open_tickers=1&'))

    def test_database_failure_preserves_input(self):
        with patch.object(store, 'change_ticker', side_effect=sqlite3.OperationalError('locked')):
            status, body, _ = self.change('add', ticker='EXAIX')
        self.assertEqual(status, 400)
        self.assertIn('value="EXAIX"', body)
        with contextlib.closing(self.conn()) as conn:
            self.assertEqual(store.get_fund(conn, '1')['ticker'], '')
