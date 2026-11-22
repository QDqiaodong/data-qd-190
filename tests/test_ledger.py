import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(ROOT / 'src'))

BOARD = ['2c', '3d', '4h', '8s', '9c']


def hand_one():
    """Hand 1: A's 5-6 straight beats B's kings for a 5/5 pot."""
    return {
        'board': BOARD,
        'button_seat': 0,
        'players': [
            {'id': 'A', 'seat': 0, 'committed': 5, 'folded': False, 'hole': ['5c', '6c']},
            {'id': 'B', 'seat': 1, 'committed': 5, 'folded': False, 'hole': ['Kh', 'Ks']},
        ],
    }


def hand_two():
    """Hand 2: A commits 12, B 5; B wins 10 and A gets 7 back."""
    return {
        'board': BOARD,
        'button_seat': 0,
        'players': [
            {'id': 'A', 'seat': 0, 'committed': 12, 'folded': False, 'hole': ['Kh', 'Ks']},
            {'id': 'B', 'seat': 1, 'committed': 5, 'folded': False, 'hole': ['5c', '6c']},
        ],
    }


def hand_one_b_wins():
    result = hand_one()
    result['players'][0].update(hole=['Kh', 'Ks'])
    result['players'][1].update(hole=['5c', '6c'])
    result['button_seat'] = 1
    return result


def hand_two_a_wins():
    result = hand_two()
    result['button_seat'] = 1
    result['players'][0].update(seat=1, committed=5, hole=['5c', '6c'])
    result['players'][1].update(seat=0, committed=5, hole=['Kh', 'Ks'])
    return result


def hand_with_suits_changed():
    """Same winners and nets as hand_one, but different card evidence."""
    result = hand_one()
    result['players'][0].update(hole=['5h', '6h'])
    return result


class LedgerCli:
    def __init__(self, test, state):
        self.test = test
        self.state = Path(state)

    def run(self, *arguments, expect=0):
        env = dict(os.environ)
        env['PYTHONPATH'] = str(ROOT / 'src')
        completed = subprocess.run(
            [sys.executable, '-m', 'showdown_pots.ledger_cli', '--state', str(self.state), *arguments],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
        )
        self.test.assertEqual(
            expect, completed.returncode,
            msg=f'argv={arguments} stderr={completed.stderr!r} stdout={completed.stdout!r}',
        )
        if expect != 0:
            self.test.assertEqual('', completed.stdout)
            self.test.assertIn('error:', completed.stderr)
            return None
        return json.loads(completed.stdout)

    def init(self, balances=None):
        return self.run('init', '--source', str(self.write({'opening_balances': balances or {'A': 10, 'B': 10}})))

    def apply(self, edits, expect=0):
        return self.run('apply', '--source', str(self.write({'edits': edits})), expect=expect)

    def preview(self):
        return self.run('preview')

    def release(self, request_id, through, expect=0):
        return self.run('release', '--request-id', request_id, '--through', str(through), expect=expect)

    def read_release(self, request_id, expect=0):
        return self.run('read-release', '--request-id', request_id, expect=expect)

    def write(self, value):
        descriptor, name = tempfile.mkstemp(dir=str(self.state.parent), suffix='.json')
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(value, stream)
        return Path(name)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name) / 'ledger.db'
        self.cli = LedgerCli(self, self.state)

    def setup_two_hands(self):
        self.cli.init()
        self.assertEqual(self.cli.apply([
            {'hand_no': 2, 'expected_revision': 0, 'source': hand_two()},
            {'hand_no': 1, 'expected_revision': 0, 'source': hand_one()},
        ]), {'head': 2, 'revisions': [
            {'hand_no': 1, 'revision': 1},
            {'hand_no': 2, 'revision': 1},
        ]})

    def digest(self):
        return hashlib.sha256(self.state.read_bytes()).hexdigest()

    def test_correction_chain_and_immutable_releases(self):
        self.setup_two_hands()

        preview = self.cli.preview()
        self.assertEqual(preview['head'], 2)
        self.assertIsNone(preview['blocking'])
        self.assertEqual([h['hand_no'] for h in preview['hands']], [1, 2])
        self.assertEqual(preview['hands'][0]['opening_balances'], {'A': 10, 'B': 10})
        self.assertEqual(preview['hands'][0]['closing_balances'], {'A': 15, 'B': 5})
        self.assertEqual(preview['hands'][1]['opening_balances'], {'A': 15, 'B': 5})
        self.assertEqual(preview['hands'][1]['closing_balances'], {'A': 10, 'B': 10})
        reports = [entry['report'] for entry in preview['hands']]
        self.assertEqual(reports[1]['uncalled_returns'],
                         [{'player_id': 'A', 'from_exclusive': 5, 'to_inclusive': 12, 'amount': 7}])
        self.assertEqual([p['net'] for p in reports[1]['players']], [-5, 5])
        self.assertEqual([p['net'] for p in reports[0]['players']], [5, -5])

        first = self.cli.release('R1', 2)
        self.assertEqual(first['previous_request_id'], None)
        self.assertEqual(first['closing_balances'], {'A': 10, 'B': 10})
        self.assertEqual(first['postings'], [
            {'hand_no': 1, 'player_id': 'A', 'delta': 5},
            {'hand_no': 1, 'player_id': 'B', 'delta': -5},
            {'hand_no': 2, 'player_id': 'A', 'delta': -5},
            {'hand_no': 2, 'player_id': 'B', 'delta': 5},
        ])
        self.assertEqual([h['revision'] for h in first['hands']], [1, 1])
        self.assertEqual(first['hands'][1]['source'], hand_two())
        self.assertEqual(first['hands'][1]['report']['uncalled_returns'][0]['amount'], 7)

        # Hand 1 correction: B wins.  It is saved even though hand 2 then blocks.
        self.assertEqual(self.cli.apply([
            {'hand_no': 1, 'expected_revision': 1, 'source': hand_one_b_wins()},
        ]), {'head': 2, 'revisions': [{'hand_no': 1, 'revision': 2}]})

        preview = self.cli.preview()
        self.assertEqual([(h['hand_no'], h['revision']) for h in preview['hands']], [(1, 2)])
        self.assertEqual(preview['revisions'], [
            {'hand_no': 1, 'revision': 2},
            {'hand_no': 2, 'revision': 1},
        ])
        self.assertEqual(preview['blocking'], {
            'hand_no': 2,
            'revision': 1,
            'shortfalls': [
                {'player_id': 'A', 'opening_balance': 5, 'committed': 12},
            ],
        })

        self.cli.release('R2', 2, expect=2)  # cannot pass the usable prefix
        self.cli.release('R2', 1, expect=2)  # cannot shorten below R1

        # Original request is returned verbatim, without new postings or checks.
        self.assertEqual(self.cli.release('R1', 2), first)
        self.assertEqual(self.cli.read_release('R1'), first)

        # Hand 2 correction: both commit 5, A wins.  Release can resume.
        self.assertEqual(self.cli.apply([
            {'hand_no': 2, 'expected_revision': 1, 'source': hand_two_a_wins()},
        ]), {'head': 2, 'revisions': [{'hand_no': 2, 'revision': 2}]})
        self.assertIsNone(self.cli.preview()['blocking'])

        second = self.cli.release('R2', 2)
        self.assertEqual(second['previous_request_id'], 'R1')
        self.assertEqual(second['through'], 2)
        self.assertEqual(second['closing_balances'], {'A': 10, 'B': 10})
        self.assertEqual(second['postings'], [
            {'hand_no': 1, 'player_id': 'A', 'delta': -10},
            {'hand_no': 1, 'player_id': 'B', 'delta': 10},
            {'hand_no': 2, 'player_id': 'A', 'delta': 10},
            {'hand_no': 2, 'player_id': 'B', 'delta': -10},
        ])
        self.assertEqual([(h['hand_no'], h['revision']) for h in second['hands']],
                         [(1, 2), (2, 2)])
        self.assertEqual([h['source'] for h in second['hands']],
                         [hand_one_b_wins(), hand_two_a_wins()])
        self.assertEqual([p['net'] for p in second['hands'][0]['report']['players']], [-5, 5])
        self.assertEqual([p['net'] for p in second['hands'][1]['report']['players']], [-5, 5])
        self.assertEqual(second['hands'][1]['report']['uncalled_returns'], [])
        self.assertEqual(second['hands'][1]['report']['pots'][0]['winner_ids_in_odd_chip_order'],
                         ['A'])

        # Reading back each statement shows the versions adopted at that time.
        self.assertEqual(self.cli.read_release('R1'), first)
        self.assertEqual(self.cli.read_release('R2'), second)
        self.assertEqual([h['revision'] for h in self.cli.read_release('R1')['hands']], [1, 1])
        self.assertEqual([h['revision'] for h in self.cli.read_release('R2')['hands']], [2, 2])

        # The accounting postings replay from the roster opening balances.
        balances = {'A': 10, 'B': 10}
        for release in (first, second):
            for posting in release['postings']:
                balances[posting['player_id']] += posting['delta']
            self.assertEqual(balances, release['closing_balances'])
            self.assertEqual(sum(p['delta'] for p in release['postings']), 0)

    def test_batch_is_atomic(self):
        self.cli.init()
        self.assertEqual(self.cli.apply([
            {'hand_no': 1, 'expected_revision': 0, 'source': hand_one()},
        ])['head'], 1)
        before = self.digest()

        bad_card = hand_two()
        bad_card['players'][0]['hole'] = ['Kh', '2c']  # duplicate with the board
        self.cli.apply([
            {'hand_no': 1, 'expected_revision': 1, 'source': hand_one_b_wins()},
            {'hand_no': 2, 'expected_revision': 0, 'source': bad_card},
        ], expect=2)
        self.assertEqual(self.digest(), before)

        # One stale revision rejects the otherwise valid append.
        self.cli.apply([
            {'hand_no': 1, 'expected_revision': 5, 'source': hand_one_b_wins()},
            {'hand_no': 2, 'expected_revision': 0, 'source': hand_two()},
        ], expect=2)
        self.assertEqual(self.digest(), before)

        # A hand whose input cannot settle rejects the batch.
        dead = hand_two()
        for player in dead['players']:
            player.update(folded=True, hole=[])
        self.cli.apply([
            {'hand_no': 2, 'expected_revision': 0, 'source': hand_two()},
            {'hand_no': 3, 'expected_revision': 0, 'source': dead},
        ], expect=2)
        self.assertEqual(self.digest(), before)

        # New hands must be consecutive, unique, in range and in the roster.
        for edits in (
            [{'hand_no': 3, 'expected_revision': 0, 'source': hand_two()}],
            [{'hand_no': 2, 'expected_revision': 0, 'source': hand_two()},
             {'hand_no': 2, 'expected_revision': 0, 'source': hand_two()}],
            [{'hand_no': 0, 'expected_revision': 0, 'source': hand_two()}],
            [{'hand_no': 1001, 'expected_revision': 0, 'source': hand_two()}],
            [{'hand_no': 1, 'expected_revision': 0, 'source': hand_two()}],
        ):
            self.cli.apply(edits, expect=2)
        outsider = hand_two()
        outsider['players'][1]['id'] = 'C'
        self.cli.apply([
            {'hand_no': 2, 'expected_revision': 0, 'source': hand_two()},
            {'hand_no': 3, 'expected_revision': 0, 'source': outsider},
        ], expect=2)
        self.assertEqual(self.digest(), before)

        preview = self.cli.preview()
        self.assertEqual(preview['head'], 1)
        self.assertEqual(preview['revisions'], [{'hand_no': 1, 'revision': 1}])

    def test_release_conflicts_unknowns_and_no_partial_writes(self):
        self.setup_two_hands()
        self.cli.release('R1', 2)

        self.cli.release('R1', 1, expect=2)       # same id, different through
        self.cli.release('R2', 1, expect=2)       # shorter than the last release
        self.cli.release('R2', 3, expect=2)       # beyond the usable prefix
        self.cli.release('bad id', 2, expect=2)   # identifier format
        self.assertEqual(self.cli.read_release('missing', expect=2), None)

        unknown = Path(self.directory.name) / 'missing.db'
        completed = subprocess.run(
            [sys.executable, '-m', 'showdown_pots.ledger_cli', '--state', str(unknown), 'preview'],
            cwd=str(ROOT), env={**os.environ, 'PYTHONPATH': str(ROOT / 'src')},
            capture_output=True, text=True,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, '')
        self.assertFalse(unknown.exists())

        # An existing ledger is never overwritten.
        before = self.digest()
        self.cli.run('init', '--source', str(self.cli.write(
            {'opening_balances': {'A': 999, 'B': 10}})), expect=2)
        self.assertEqual(self.digest(), before)

        # A truncated or foreign file is not treated as a ledger.
        corrupt = Path(self.directory.name) / 'corrupt.db'
        corrupt.write_bytes(b'not a ledger')
        completed = subprocess.run(
            [sys.executable, '-m', 'showdown_pots.ledger_cli', '--state', str(corrupt), 'preview'],
            cwd=str(ROOT), env={**os.environ, 'PYTHONPATH': str(ROOT / 'src')},
            capture_output=True, text=True,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, '')

    def test_evidence_change_without_net_change_still_appears(self):
        self.setup_two_hands()
        first = self.cli.release('R1', 2)
        self.cli.apply([
            {'hand_no': 1, 'expected_revision': 1, 'source': hand_with_suits_changed()},
        ])
        second = self.cli.release('R2', 2)
        self.assertEqual(second['postings'], [])
        self.assertEqual(second['closing_balances'], first['closing_balances'])
        self.assertEqual([(h['hand_no'], h['revision']) for h in second['hands']],
                         [(1, 2), (2, 1)])
        self.assertEqual(second['hands'][0]['source'], hand_with_suits_changed())
        self.assertEqual(second['hands'][0]['report']['players'][0]['hand']['best_five'],
                         ['2c', '3d', '4h', '5h', '6h'])
        self.assertEqual(second['hands'][0]['report']['players'][0]['net'], 5)
        self.assertEqual(self.cli.read_release('R1'), first)

    def test_roster_identity_absence_seat_changes_and_folded_commitments(self):
        self.cli.init({'A': 10, 'B': 10, 'C': 10})
        one = hand_one()
        one['players'][0]['seat'] = 5       # seating does not change identity
        one['players'][1]['seat'] = 7
        one['button_seat'] = 3
        self.cli.apply([{'hand_no': 1, 'expected_revision': 0, 'source': one}])
        self.assertEqual(self.cli.preview()['hands'][0]['closing_balances'],
                         {'A': 15, 'B': 5, 'C': 10})

        # A folded player's full commitment still needs its opening balance.
        folded = hand_two()
        folded['players'].append(
            {'id': 'C', 'seat': 2, 'committed': 11, 'folded': True, 'hole': []})
        folded['button_seat'] = 2
        self.cli.apply([{'hand_no': 2, 'expected_revision': 0, 'source': folded}])
        preview = self.cli.preview()
        self.assertEqual([h['hand_no'] for h in preview['hands']], [1])
        self.assertEqual(preview['blocking']['shortfalls'], [
            {'player_id': 'C', 'opening_balance': 10, 'committed': 11},
        ])
        self.cli.release('R1', 2, expect=2)
        released = self.cli.release('R1', 1)
        self.assertEqual(released['closing_balances'], {'A': 15, 'B': 5, 'C': 10})
        self.assertEqual(released['postings'], [
            {'hand_no': 1, 'player_id': 'A', 'delta': 5},
            {'hand_no': 1, 'player_id': 'B', 'delta': -5},
        ])

    def test_apply_same_source_increments_revision(self):
        self.setup_two_hands()
        self.assertEqual(self.cli.apply([
            {'hand_no': 1, 'expected_revision': 1, 'source': hand_one()},
        ]), {'head': 2, 'revisions': [{'hand_no': 1, 'revision': 2}]})
        self.assertEqual(self.cli.preview()['revisions'][0],
                         {'hand_no': 1, 'revision': 2})

    def test_incremental_release_only_posts_new_range(self):
        self.setup_two_hands()
        first = self.cli.release('R1', 1)
        self.assertEqual([h['hand_no'] for h in first['hands']], [1])
        self.assertEqual(first['closing_balances'], {'A': 15, 'B': 5})
        second = self.cli.release('R2', 2)
        self.assertEqual(second['previous_request_id'], 'R1')
        self.assertEqual(second['postings'], [
            {'hand_no': 2, 'player_id': 'A', 'delta': -5},
            {'hand_no': 2, 'player_id': 'B', 'delta': 5},
        ])
        self.assertEqual(second['closing_balances'], {'A': 10, 'B': 10})

    def test_init_roster_validation_and_max_size(self):
        for balances in (
            {'A': 10},
            {f'P{i}': 1 for i in range(101)},
            {'A': -1, 'B': 1},
            {'A': 1.5, 'B': 1},
            {'bad id': 1, 'B': 1},
            {'A': 10 ** 9 + 1, 'B': 1},
        ):
            self.cli.run('init', '--source', str(self.cli.write(
                {'opening_balances': balances})), expect=2)
            self.assertFalse(self.state.exists())
        self.assertEqual(self.cli.init({f'P{i}': 10 ** 9 for i in range(100)}), {'head': 0})
        self.assertEqual(self.cli.preview()['hands'], [])
        self.assertEqual(self.cli.preview()['head'], 0)

    def test_release_posts_only_changed_hand_player(self):
        self.cli.init({'A': 10, 'B': 10, 'C': 10})
        three = hand_one()
        three['players'].append(
            {'id': 'C', 'seat': 2, 'committed': 5, 'folded': False, 'hole': ['Ah', 'Ad']})
        three['button_seat'] = 0
        self.cli.apply([{'hand_no': 1, 'expected_revision': 0, 'source': three}])
        first = self.cli.release('R1', 1)
        self.assertEqual([p['net'] for p in first['hands'][0]['report']['players']],
                         [10, -5, -5])
        self.assertEqual(sorted(first['closing_balances'].items()),
                         [('A', 20), ('B', 5), ('C', 5)])

        # B now wins instead of A; C's net is unchanged, so C is not posted.
        corrected = hand_one()
        corrected['players'][0].update(hole=['Kh', 'Ks'])
        corrected['players'][1].update(hole=['5c', '6c'])
        corrected['players'].append(
            {'id': 'C', 'seat': 2, 'committed': 5, 'folded': False, 'hole': ['Ah', 'Ad']})
        corrected['button_seat'] = 0
        self.cli.apply([{'hand_no': 1, 'expected_revision': 1, 'source': corrected}])
        second = self.cli.release('R2', 1)
        self.assertEqual(second['postings'], [
            {'hand_no': 1, 'player_id': 'A', 'delta': -15},
            {'hand_no': 1, 'player_id': 'B', 'delta': 15},
        ])
        self.assertEqual(second['hands'][0]['source'], corrected)
        self.assertEqual(second['closing_balances'], {'A': 5, 'B': 20, 'C': 5})
        self.assertEqual(self.cli.read_release('R1'), first)

        balances = {'A': 10, 'B': 10, 'C': 10}
        for release in (first, second):
            for posting in release['postings']:
                balances[posting['player_id']] += posting['delta']
            self.assertEqual(balances, release['closing_balances'])


if __name__ == '__main__':
    unittest.main()
