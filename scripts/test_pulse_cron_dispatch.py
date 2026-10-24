"""Dispatcher contract: RESULT parsing, jsonl rows, one page per failure streak, role dispatch."""
import importlib.util
from importlib.machinery import SourceFileLoader
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_loader('dispatch', SourceFileLoader('dispatch', str(HERE / 'pulse-cron-dispatch.py')))
d = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(d)

FAKE_RUNNER = '''#!/bin/sh
echo "$@" > "$RUNNER_ARGS"
meta=""; prompt=""; while [ $# -gt 0 ]; do [ "$1" = "--meta" ] && meta="$2"; [ "$1" = "--prompt-file" ] && prompt="$2"; shift; done
cp "$prompt" "$RUNNER_PROMPT"
echo '{"provider": "codex:gpt-6-luna"}' > "$meta"
printf 'working\\n%s\\n' "$FAKE_LAST"
exit "${FAKE_EXIT:-0}"
'''


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / 'crons').mkdir()
        runner = self.tmp / 'g-agent-run'
        runner.write_text(FAKE_RUNNER)
        runner.chmod(runner.stat().st_mode | stat.S_IEXEC)
        env = {'RUNNER_ARGS': str(self.tmp / 'args'), 'RUNNER_PROMPT': str(self.tmp / 'prompt'), 'FAKE_LAST': 'RESULT: ok - did it', 'FAKE_EXIT': '0'}
        for name, value in env.items():
            patcher = patch.dict(os.environ, {name: value})
            patcher.start()
            self.addCleanup(patcher.stop)
        for attr, value in {'AGENT_RUN': str(runner), 'STATE_DIR': self.tmp, 'PULSE_DIR': self.tmp, 'CRONS_DIR': self.tmp / 'crons',
                            'RUNS_FILE': self.tmp / 'cron-runs.json', 'RUNS_LOG': self.tmp / 'cron-runs.jsonl',
                            'LOG_FILE': self.tmp / 'cron.log', 'STREAKS_FILE': self.tmp / 'streaks.json'}.items():
            patcher = patch.object(d, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.pages = []
        self.page_ok = True
        patcher = patch.object(d, 'page', lambda level, msg, key=None, clear=False: self.pages.append((level, key, clear)) or self.page_ok)
        patcher.start()
        self.addCleanup(patcher.stop)

    def cron(self, **over):
        return {'name': 'job', 'role': 'bulk', 'body': 'do the thing', 'connectors': False, 'gate': '', 'workdir': '', **over}

    def rows(self):
        return [json.loads(line) for line in (self.tmp / 'cron-runs.jsonl').read_text().splitlines()]

    def run_job(self, **over):
        return d.execute(self.cron(**over), {})

    def test_parse_result_takes_last_result_line_and_flags_missing(self):
        self.assertEqual(d.parse_result('x\nRESULT: noop - nothing new\n'), ('noop', 'nothing new'))
        self.assertEqual(d.parse_result('RESULT: ok - a\nRESULT: fail - b'), ('fail', 'b'))
        self.assertEqual(d.parse_result('I could not reach it, shall I retry?')[0], 'missing')

    def test_run_goes_through_runner_with_role_and_appends_contract_and_row(self):
        row = self.run_job()
        args = (self.tmp / 'args').read_text()
        self.assertIn('--role bulk', args)
        self.assertIn('--tools', args)
        self.assertNotIn('--connectors', args)
        prompt = (self.tmp / 'prompt').read_text()
        self.assertTrue(prompt.startswith('do the thing'))
        self.assertIn('RESULT: ok|noop|fail', prompt)
        self.assertEqual((row['result'], row['provider'], row['model'], row['exit']), ('ok', 'codex', 'gpt-6-luna', 0))
        logged = self.rows()[0]
        self.assertEqual(set(logged), {'name', 'role', 'started', 'finished', 'duration_s', 'exit', 'provider', 'model', 'result', 'summary'})
        self.assertEqual(self.pages, [])

    def test_connectors_flag_is_forwarded(self):
        self.run_job(connectors=True)
        self.assertIn('--connectors', (self.tmp / 'args').read_text())

    def test_exit_ok_without_result_line_is_missing_and_pages(self):
        os.environ['FAKE_LAST'] = 'Would you like me to authorize the connector?'
        row = self.run_job()
        self.assertEqual(row['result'], 'missing')
        self.assertEqual(self.pages[0][:2], ('error', 'pulse-cron:job'))

    def test_runner_failure_exit_is_missing_with_exit_code(self):
        os.environ['FAKE_EXIT'] = '3'
        row = self.run_job()
        self.assertEqual((row['result'], row['exit']), ('missing', 3))
        self.assertEqual(len(self.pages), 1)

    def test_one_page_per_failure_streak_and_clear_on_recovery(self):
        os.environ['FAKE_LAST'] = 'RESULT: fail - source down'
        for _ in range(3):
            self.run_job()
        self.assertEqual(len(self.pages), 1)
        os.environ['FAKE_LAST'] = 'RESULT: noop - nothing'
        self.run_job()
        self.assertEqual(self.pages[-1], ('info', 'pulse-cron:job', True))
        os.environ['FAKE_LAST'] = 'RESULT: fail - down again'
        self.run_job()
        self.assertEqual(len(self.pages), 3)

    def test_unsent_page_is_retried_on_the_next_failure(self):
        self.page_ok = False
        os.environ['FAKE_LAST'] = 'RESULT: fail - down'
        self.run_job()
        self.run_job()
        self.assertEqual(len(self.pages), 2)
        self.page_ok = True
        self.run_job()
        self.run_job()
        self.assertEqual(len(self.pages), 3)

    def test_gate_no_work_is_noop_with_zero_model_calls(self):
        row = self.run_job(gate='echo "(no new mail)"; exit 3')
        self.assertEqual((row['result'], row['provider'], row['exit'], row['summary']), ('noop', 'gate', 0, '(no new mail)'))
        self.assertFalse((self.tmp / 'args').exists())
        self.assertEqual(self.pages, [])

    def test_gate_work_passes_its_stdout_into_the_prompt_and_workdir_is_forwarded(self):
        row = self.run_job(gate='echo MAIL-BODY-42', workdir='/tmp/scratch-x')
        self.assertEqual(row['result'], 'ok')
        self.assertIn('MAIL-BODY-42', (self.tmp / 'prompt').read_text())
        self.assertIn('--workdir /tmp/scratch-x', (self.tmp / 'args').read_text())

    def test_gate_error_fails_and_pages_instead_of_silently_skipping(self):
        row = self.run_job(gate='echo imap down >&2; exit 1')
        self.assertEqual((row['result'], row['provider']), ('fail', 'gate'))
        self.assertEqual(len(self.pages), 1)

    def test_missing_role_fails_without_running_anything(self):
        row = self.run_job(role='')
        self.assertEqual(row['result'], 'fail')
        self.assertFalse((self.tmp / 'args').exists())

    def test_weekday_field_counts_from_sunday_like_cron(self):
        from datetime import datetime
        monday, saturday, sunday = datetime(2026, 10, 26, 9), datetime(2026, 10, 31, 9), datetime(2026, 11, 1, 9)
        self.assertTrue(d.schedule_matches('0 9 * * 1-5', monday))
        self.assertFalse(d.schedule_matches('0 9 * * 1-5', saturday))
        self.assertTrue(d.schedule_matches('0 9 * * 0', sunday))
        self.assertTrue(d.schedule_matches('0 9 * * 7', sunday))
        self.assertTrue(d.schedule_matches('0 9 * * 5-7', sunday))
        self.assertTrue(d.schedule_matches('0 9 * * MON-FRI', monday))
        self.assertFalse(d.schedule_matches('0 9 * * mon-fri', sunday))
        self.assertTrue(d.schedule_matches('0 9 * oct-nov sun', sunday))

    def test_both_day_fields_restricted_means_either_matches(self):
        from datetime import datetime
        self.assertTrue(d.schedule_matches('0 9 1 * 1', datetime(2026, 10, 26, 9)))   # a Monday, not the 1st
        self.assertTrue(d.schedule_matches('0 9 1 * 1', datetime(2026, 11, 1, 9)))    # the 1st, a Sunday
        self.assertFalse(d.schedule_matches('0 9 1 * 1', datetime(2026, 10, 27, 9)))  # neither
        self.assertFalse(d.schedule_matches('0 9 1 * *', datetime(2026, 10, 26, 9)))

    def test_unreadable_schedule_fails_once_and_later_jobs_still_run(self):
        from datetime import datetime, timezone, timedelta
        for name, schedule in (('a-bad', '0 9 * * funday'), ('b-good', '* * * * *')):
            (self.tmp / 'crons' / f'{name}.md').write_text(
                f'---\nname: {name}\nschedule: "{schedule}"\nrunner: agent\nrole: bulk\nenabled: true\n---\nbody\n')
        earlier = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        (self.tmp / 'cron-runs.json').write_text(json.dumps({'a-bad': earlier, 'b-good': earlier}))
        with patch.object(d, 'LOCK_FILE', self.tmp / 'dispatch.lock'), patch('sys.argv', ['dispatch']):
            d.main()
            d.main()
        rows = self.rows()
        self.assertEqual([(r['name'], r['result']) for r in rows if r['name'] == 'a-bad'], [('a-bad', 'fail')])
        self.assertIn('unreadable schedule', rows[0]['summary'])
        self.assertIn(('b-good', 'ok'), [(r['name'], r['result']) for r in rows])
        self.assertEqual(self.pages, [('error', 'pulse-cron:a-bad', False)])

    def test_legacy_claude_runner_is_accepted_and_load_crons_reads_role(self):
        (self.tmp / 'crons' / 'a.md').write_text('---\nname: a\nschedule: "0 5 * * *"\nrunner: claude\nrole: verify\nenabled: true\n---\nbody\n')
        (self.tmp / 'crons' / '_t.md').write_text('---\nname: t\n---\n')
        crons = d.load_crons()
        self.assertEqual([(c['name'], c['role'], c['enabled']) for c in crons], [('a', 'verify', True)])
        self.assertIn(crons[0]['runner'], d.RUNNERS)


if __name__ == '__main__':
    unittest.main()
