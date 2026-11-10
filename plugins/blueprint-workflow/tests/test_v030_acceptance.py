"""Domain-neutral durability, migration and long-running lifecycle acceptance."""
from copy import deepcopy
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from blueprint_workflow.store import Store
from blueprint_workflow.hooks import handle_hook
from test_store import example_workflow


class Acceptance030Tests(unittest.TestCase):
    def test_analysis_failure_does_not_stop_either_capture_route(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            sid = store.start_session('Task', 'Produce the requested output', 'original-chat')['session_id']
            job = store.claim_job('worker')
            store.fail_job(job['job_id'], 'worker', 'Selected engine unavailable', retry_delay=None)
            state = store.status(sid)
            self.assertEqual(state['analysis_state'], 'failed')
            self.assertEqual(state['capture_state'], 'recording')
            store.append_events(sid, [{'kind': 'observation', 'summary': 'Work continued while analysis was unavailable'}])
            handle_hook({'session_id': 'original-chat', 'hook_event_name': 'UserPromptSubmit',
                         'turn_id': 'later', 'prompt': 'Keep the confirmed output requirement.'},
                        directory, store=store, worker_launcher=lambda _: None)
            store.drain_capture(sid)
            self.assertEqual(store.status(sid)['latest_seq'], 3)
            self.assertIsNone(store.claim_job('worker'))
            store.retry_analysis(sid)
            job = store.claim_job('worker')
            self.assertEqual((job['from_seq'], job['through_seq']), (1, 3))

    def test_receipt_then_finish_drains_every_accepted_event(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            sid = store.start_session('Task', 'Produce the requested output')['session_id']
            for i in range(8):
                store.capture_enqueue(sid, [{'kind': 'observation', 'summary': f'Phase {i}', 'client_event_id': f'capture-{i}'}])
            state = store.finish_session(sid, 'The demonstrated work is complete')
            self.assertEqual(state['final_seq'], 10)
            self.assertEqual(len(store.get_events(sid)), 10)
            self.assertEqual(state['capture_state'], 'closed')
            with self.assertRaises((ValueError, RuntimeError)):
                store.capture_enqueue(sid, [{'kind': 'observation', 'summary': 'Late work'}])

    def test_new_human_constraint_prevents_stale_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            sid = store.start_session('Task', 'Produce the requested output')['session_id']
            job = store.claim_job('worker')
            workflow = example_workflow([job['events'][0]['id']])
            store.append_events(sid, [{'kind': 'correction', 'actor': 'human', 'summary': 'Use the explicitly revised method.'}])
            with self.assertRaisesRegex(ValueError, 'stale'):
                store.commit_job(job['job_id'], 'worker', {'workflow': workflow, 'change_summary': 'Old analysis'})
            self.assertEqual(store.status(sid)['processed_seq'], 0)

    def test_14_days_10000_events_survive_restarts_and_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            sid = store.start_session('Long task', 'Develop a reusable method over multiple work periods', 'one-chat')['session_id']
            first_id = store.get_events(sid)[0]['id']
            total = 10000
            for day in range(14):
                start, end = total * day // 14, total * (day + 1) // 14
                events = [{'kind': 'observation', 'summary': f'Work period {day + 1}, observation {i}',
                           'client_event_id': f'period-{i}', 'details': {'simulated_day': day + 1}}
                          for i in range(start, end)]
                store.append_events(sid, events)
                store.append_events(sid, events[:1])
                # Each new instance is the durable restart boundary, not a mock.
                store = Store(directory)
            self.assertEqual(store.status(sid)['latest_seq'], 10001)
            while (job := store.claim_job('acceptance')) is not None:
                workflow = deepcopy(job.get('previous_draft')) or example_workflow([first_id])
                store.commit_job(job['job_id'], 'acceptance', {'workflow': workflow, 'change_summary': 'Preserved the current method from new evidence'})
            state = store.status(sid)
            self.assertEqual((state['latest_seq'], state['processed_seq']), (10001, 10001))
            ids = []
            cursor = 0
            while (page := store.get_events(sid, cursor, 1000)):
                ids.extend(e['id'] for e in page)
                cursor = page[-1]['seq']
            self.assertEqual(len(ids), len(set(ids)))
            self.assertEqual(len(ids), 10001)

    def test_additive_v2_migration_keeps_event_bytes_and_creates_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            sid = store.start_session('Existing task', 'Retain the existing work', 'old-chat')['session_id']
            store.append_events(sid, [{'kind': 'observation', 'summary': 'Previously saved evidence', 'client_event_id': 'prior'}])
            with closing(sqlite3.connect(store.db_path)) as db, db:
                original = db.execute('SELECT id,seq,payload FROM events ORDER BY seq').fetchall()
                for column in ('capture_state', 'analysis_state', 'constraint_revision'):
                    db.execute(f'ALTER TABLE sessions DROP COLUMN {column}')
                db.execute('ALTER TABLE jobs DROP COLUMN constraint_revision')
            upgraded = Store(directory)
            self.assertTrue(Path(upgraded.migration_backup).is_file())
            with closing(sqlite3.connect(upgraded.db_path)) as db:
                self.assertEqual(db.execute('SELECT id,seq,payload FROM events ORDER BY seq').fetchall(), original)
            self.assertEqual(upgraded.status(sid)['capture_state'], 'recording')
            self.assertEqual(upgraded.status(sid)['capture']['legacy_capture']['through_seq'], 2)
            self.assertFalse(upgraded.status(sid)['capture']['legacy_capture']['original_unsaved_content_recoverable'])


if __name__ == '__main__':
    unittest.main()
