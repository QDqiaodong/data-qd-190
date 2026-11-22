"""Regression tests for the two-source ``showdown-reconcile`` command.

The scenarios follow the operations acceptance table: one shared table tag
may repeat, the clock offset must be one integer for the whole batch, and an
anchor pair is a staff-confirmed identity that has to survive into every
maximum alignment.  Chip differences are checked only for pairs that are
forced in every maximum alignment.
"""
import copy
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

from showdown_pots.reconcile import reconcile
from showdown_pots.service import process

BOARD = ['2c', '3d', '4h', '8s', '9c']


def acceptance_hand():
    """A/B split evenly; A's straight beats B's pair, so A +5, B -5."""
    return {
        'board': list(BOARD), 'button_seat': 0,
        'players': [
            {'id': 'A', 'seat': 0, 'committed': 5, 'folded': False,
             'hole': ['5c', '6c']},
            {'id': 'B', 'seat': 1, 'committed': 5, 'folded': False,
             'hole': ['Kh', 'Ks']},
        ],
    }


GOOD = {'A': 5, 'B': -5}


def left(record_id, seq, ts, tag, hand=None):
    return {'id': record_id, 'seq': seq, 'ts': ts, 'tag': tag,
            'hand': copy.deepcopy(hand or acceptance_hand())}


def right(record_id, seq, ts, tag, net=None):
    return {'id': record_id, 'seq': seq, 'ts': ts, 'tag': tag,
            'net': copy.deepcopy(GOOD if net is None else net)}


def batch(left_records, right_records, low=-86_400_000, high=86_400_000,
          tolerance=1, anchors=(), focus=None):
    data = {
        'left': left_records, 'right': right_records,
        'clock': {'min_offset': low, 'max_offset': high, 'tolerance': tolerance},
        'anchors': list(anchors),
    }
    if focus is not None:
        data['focus_left_id'] = focus
    return data


def acceptance():
    return batch(
        [left('L0', 1, 90, 'K'), left('L1', 2, 100, 'X'),
         left('L2', 3, 110, 'Y'), left('L3', 4, 120, 'Z'),
         left('L4', 5, 130, 'W'), left('L5', 6, 140, 'V')],
        [right('R0', 1, 91, 'K', {'A': 4, 'B': -5}),
         right('R1', 2, 99, 'X', {'A': 5, 'C': 0}),
         right('R2a', 3, 109, 'Y'), right('R2b', 4, 113, 'Y'),
         right('R3', 5, 119, 'Z'), right('R4', 6, 133, 'W'),
         right('R5', 7, 143, 'V')],
    )


def reordered(data, seed):
    """Shuffle both input arrays without changing their batch semantics."""
    result = copy.deepcopy(data)
    import random
    rng = random.Random(seed)
    rng.shuffle(result['left'])
    rng.shuffle(result['right'])
    return result


def canonical(result):
    return json.dumps(result, sort_keys=True, ensure_ascii=False)


class ReconcileCoreTests(unittest.TestCase):
    def test_acceptance_offsets_holes_and_forced_difference(self):
        result = reconcile(acceptance())
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['max_pairs'], 4)
        # Offset 1 reaches no maximum alignment and must not be merged in.
        self.assertEqual(result['offsets'], [{'from': 0, 'to': 0},
                                             {'from': 2, 'to': 2}])
        self.assertEqual(result['reconciled'], [{
            'left_id': 'L0', 'right_id': 'R0',
            'differences': [{'player_id': 'A', 'expected': 5, 'observed': 4}],
        }])
        by_id = {item['id']: item for item in result['left']}
        self.assertEqual(by_id['L1']['partners'], ['R1'])
        self.assertTrue(by_id['L1']['may_be_unmatched'])
        self.assertEqual(by_id['L2']['partners'], ['R2a', 'R2b'])
        self.assertFalse(by_id['L2']['may_be_unmatched'])
        self.assertEqual(by_id['L3']['partners'], ['R3'])
        self.assertEqual(result['examples'], [])

    def test_focus_witnesses_use_both_duplicate_partners(self):
        result = reconcile(acceptance() | {'focus_left_id': 'L2'})
        examples = result['examples']
        self.assertEqual(len(examples), 2)
        self.assertEqual({example['offset'] for example in examples}, {0, 2})
        picked = set()
        for example in examples:
            self.assertEqual(len(example['pairs']), 4)
            focus_pairs = [pair for pair in example['pairs']
                           if pair['left_id'] == 'L2']
            self.assertEqual(len(focus_pairs), 1)
            picked.add(focus_pairs[0]['right_id'])
        self.assertEqual(picked, {'R2a', 'R2b'})

    def test_focus_with_optional_partner_needs_unmatched_witness(self):
        result = reconcile(acceptance() | {'focus_left_id': 'L1'})
        examples = result['examples']
        self.assertEqual(len(examples), 2)
        used = [{pair['left_id'] for pair in example['pairs']}
                for example in examples]
        self.assertEqual(sum('L1' in entry for entry in used), 1)
        for example in examples:
            self.assertEqual(len(example['pairs']), result['max_pairs'])

    def test_restricted_clock_keeps_only_forced_pair(self):
        result = reconcile(batch(
            acceptance()['left'], acceptance()['right'],
            low=1, high=1, tolerance=1))
        self.assertEqual(result['max_pairs'], 1)
        self.assertEqual(result['offsets'], [{'from': 1, 'to': 1}])
        self.assertEqual([item['left_id'] for item in result['reconciled']],
                         ['L0'])
        self.assertEqual(result['left'][0]['partners'], ['R0'])

    def test_anchor_forces_optional_pair_into_reconciliation(self):
        data = acceptance()
        data['anchors'] = [{'left_id': 'L3', 'right_id': 'R3'}]
        result = reconcile(data)
        self.assertEqual(result['max_pairs'], 4)
        self.assertEqual(result['offsets'], [{'from': 0, 'to': 0}])
        reconciled = {item['left_id']: item for item in result['reconciled']}
        self.assertEqual(set(reconciled), {'L0', 'L1', 'L2', 'L3'})
        self.assertEqual(reconciled['L1']['differences'], [
            {'player_id': 'B', 'expected': -5, 'observed': None},
            {'player_id': 'C', 'expected': None, 'observed': 0},
        ])
        self.assertEqual(reconciled['L3']['differences'], [])

    def test_incompatible_anchors_publish_no_alignment(self):
        data = acceptance()
        data['anchors'] = [{'left_id': 'L3', 'right_id': 'R3'},
                           {'left_id': 'L4', 'right_id': 'R4'}]
        result = reconcile(data)
        self.assertEqual(result, {
            'status': 'no_alignment', 'max_pairs': None, 'offsets': [],
            'left': [], 'right': [], 'reconciled': [], 'examples': [],
        })

    def test_fixed_clock_same_times_swapped_tags_is_ambiguous(self):
        data = batch(
            [left('LX', 1, 100, 'X'), left('LY', 2, 100, 'Y')],
            [right('RY', 1, 100, 'Y'), right('RX', 2, 100, 'X')],
            low=0, high=0, tolerance=0)
        result = reconcile(data)
        self.assertEqual(result['max_pairs'], 1)
        self.assertEqual(result['offsets'], [{'from': 0, 'to': 0}])
        self.assertEqual(result['reconciled'], [])
        by_id = {item['id']: item for item in result['left']}
        self.assertEqual(by_id['LX']['partners'], ['RX'])
        self.assertTrue(by_id['LX']['may_be_unmatched'])
        self.assertEqual(by_id['LY']['partners'], ['RY'])
        self.assertTrue(by_id['LY']['may_be_unmatched'])

    def test_reordering_input_arrays_does_not_change_report(self):
        expected = canonical(reconcile(acceptance() | {'focus_left_id': 'L2'}))
        for seed in range(5):
            shuffled = reordered(acceptance(), seed)
            shuffled['focus_left_id'] = 'L2'
            self.assertEqual(canonical(reconcile(shuffled)), expected)

    def test_no_pairs_still_ok_and_whole_window(self):
        data = batch([left('L0', 1, 0, 'K')], [right('R0', 1, 0, 'Z')],
                     low=-5, high=7, tolerance=0)
        result = reconcile(data)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['max_pairs'], 0)
        self.assertEqual(result['offsets'], [{'from': -5, 'to': 7}])
        self.assertEqual(result['reconciled'], [])
        self.assertTrue(all(item['may_be_unmatched'] for item in result['left']))
        self.assertTrue(all(not item['partners'] for item in result['right']))


class SettlementDifferenceTests(unittest.TestCase):
    def one_pair(self, hand, net):
        return batch(
            [left('L0', 1, 100, 'T', hand)],
            [right('R0', 1, 100, 'T', net)],
            low=0, high=0, tolerance=0)

    def test_repository_fixture_matches_and_differs(self):
        hand = json.loads((ROOT / 'fixtures' / 'input.json').read_text())
        expected = {'ace': 60, 'king': -50, 'straight': 60, 'fold': -70}
        result = reconcile(self.one_pair(hand, expected))
        self.assertEqual(result['reconciled'], [
            {'left_id': 'L0', 'right_id': 'R0', 'differences': []}])
        observed = dict(expected)
        observed['ace'] = 30
        result = reconcile(self.one_pair(hand, observed))
        self.assertEqual(result['reconciled'][0]['differences'], [
            {'player_id': 'ace', 'expected': 60, 'observed': 30}])

    def test_tie_odd_chip_uses_v1_order(self):
        hand = {
            'board': ['Tc', 'Jc', 'Qc', 'Kc', 'Ac'], 'button_seat': 1,
            'players': [
                {'id': 'short', 'seat': 0, 'committed': 5, 'folded': False,
                 'hole': ['2c', '3d']},
                {'id': 'after', 'seat': 1, 'committed': 5, 'folded': False,
                 'hole': ['4c', '5d']},
                {'id': 'before', 'seat': 2, 'committed': 5, 'folded': False,
                 'hole': ['6c', '7d']},
                {'id': 'fold', 'seat': 3, 'committed': 4, 'folded': True,
                 'hole': []},
            ],
        }
        # The board is a royal flush; seat order after the button decides
        # who takes the extra odd chip in the tied pot.
        report = process(hand)
        self.assertEqual(
            report['pots'][0]['winner_ids_in_odd_chip_order'],
            ['before', 'short', 'after'])
        self.assertEqual(report['pots'][0]['awards'],
                         {'before': 6, 'short': 5, 'after': 5})
        nets = {player['player_id']: player['net']
                for player in report['players']}
        self.assertEqual(nets, {'short': 1, 'after': 1, 'before': 2, 'fold': -4})
        result = reconcile(self.one_pair(hand, nets))
        self.assertEqual(result['reconciled'][0]['differences'], [])

    def test_missing_and_extra_keys_are_differences_not_errors(self):
        hand = {
            'board': list(BOARD), 'button_seat': 0,
            'players': [
                {'id': 'A', 'seat': 0, 'committed': 5, 'folded': False,
                 'hole': ['5c', '6c']},
                {'id': 'B', 'seat': 1, 'committed': 5, 'folded': False,
                 'hole': ['Kh', 'Ks']},
            ],
        }
        result = reconcile(self.one_pair(hand, {'A': 5}))
        self.assertEqual(result['reconciled'][0]['differences'], [
            {'player_id': 'B', 'expected': -5, 'observed': None}])
        result = reconcile(self.one_pair(hand, {'A': 5, 'B': -5, 'Z': 0}))
        self.assertEqual(result['reconciled'][0]['differences'], [
            {'player_id': 'Z', 'expected': None, 'observed': 0}])

    def test_unmatched_invalid_hand_is_still_an_input_error(self):
        good = acceptance()
        bad_hand = acceptance_hand()
        bad_hand['players'][0]['hole'][0] = '2c'  # duplicates a board card
        data = batch(
            [left('L0', 1, 0, 'K'), left('L1', 2, 10, 'Q', bad_hand)],
            [right('R0', 1, 0, 'K')], low=-1, high=1, tolerance=1)
        with self.assertRaises(ValueError):
            reconcile(data)
        # The same malformed hand is rejected even when nothing would match.
        data['right'] = []
        with self.assertRaises(ValueError):
            reconcile(data)

    def test_unconfirmed_pair_never_produces_differences(self):
        data = batch(
            [left('LX', 1, 100, 'X'), left('LY', 2, 100, 'Y')],
            [right('RY', 1, 100, 'Y', {'A': 1, 'B': -1}),
             right('RX', 2, 100, 'X', {'A': 2, 'B': -2})],
            low=0, high=0, tolerance=0)
        result = reconcile(data)
        self.assertEqual(result['max_pairs'], 1)
        # Only one of the two pairs fits; the other hand is optional, so no
        # pair is forced and no amount is "corrected".
        self.assertEqual(result['reconciled'], [])


class ReconcileValidationTests(unittest.TestCase):
    def test_structure_and_reference_errors(self):
        good = acceptance()
        cases = []
        for field in ('left', 'right', 'clock', 'anchors'):
            data = copy.deepcopy(good)
            del data[field]
            cases.append(data)
        data = copy.deepcopy(good)
        data['extra'] = 1
        cases.append(data)
        data = copy.deepcopy(good)
        data['clock']['tolerance'] = 1_000_001
        cases.append(data)
        data = copy.deepcopy(good)
        data['clock']['min_offset'] = 86_400_001
        cases.append(data)
        data = copy.deepcopy(good)
        data['clock']['min_offset'] = True
        cases.append(data)
        data = copy.deepcopy(good)
        data['left'][0]['id'] = 'bad id!'
        cases.append(data)
        data = copy.deepcopy(good)
        data['left'][1]['id'] = data['left'][0]['id']
        cases.append(data)
        data = copy.deepcopy(good)
        data['right'][1]['seq'] = data['right'][0]['seq']
        cases.append(data)
        data = copy.deepcopy(good)
        data['left'][0]['seq'] = 0
        cases.append(data)
        data = copy.deepcopy(good)
        data['right'][0]['net']['A'] = 9_000_000_001
        cases.append(data)
        data = copy.deepcopy(good)
        data['right'][0]['net'] = {'A': True}
        cases.append(data)
        data = copy.deepcopy(good)
        data['right'][0]['net'] = {f'P{i}': 1 for i in range(10)}
        cases.append(data)
        data = copy.deepcopy(good)
        data['anchors'] = [{'left_id': 'nope', 'right_id': 'R0'}]
        cases.append(data)
        data = copy.deepcopy(good)
        data['anchors'] = [{'left_id': 'L0', 'right_id': 'R0'},
                           {'left_id': 'L0', 'right_id': 'R1'}]
        cases.append(data)
        data = copy.deepcopy(good)
        data['anchors'] = [{'left_id': 'L0', 'right_id': 'R0'},
                           {'left_id': 'L1', 'right_id': 'R0'}]
        cases.append(data)
        data = copy.deepcopy(good)
        data['focus_left_id'] = 'nope'
        cases.append(data)
        data = copy.deepcopy(good)
        data['left'][0]['hand'] = {'board': ['2c']}
        cases.append(data)
        for data in cases:
            with self.assertRaises(ValueError, msg=json.dumps(data)):
                reconcile(data)

    def test_anchor_tag_mismatch_is_a_business_result(self):
        data = acceptance()
        data['anchors'] = [{'left_id': 'L0', 'right_id': 'R1'}]
        self.assertEqual(reconcile(data)['status'], 'no_alignment')
        data = acceptance()
        data['anchors'] = [{'left_id': 'L0', 'right_id': 'R2b'}]
        self.assertEqual(reconcile(data)['status'], 'no_alignment')

    def test_anchor_order_conflict_is_a_business_result(self):
        data = batch(
            [left('L0', 1, 100, 'X'), left('L1', 2, 100, 'Y')],
            [right('R0', 1, 100, 'Y'), right('R1', 2, 100, 'X')],
            low=0, high=0, tolerance=0,
            anchors=[{'left_id': 'L0', 'right_id': 'R1'},
                     {'left_id': 'L1', 'right_id': 'R0'}])
        self.assertEqual(reconcile(data)['status'], 'no_alignment')

    def test_anchor_clock_conflict_is_a_business_result(self):
        data = acceptance()
        data['anchors'] = [{'left_id': 'L3', 'right_id': 'R3'},
                           {'left_id': 'L4', 'right_id': 'R4'}]
        self.assertEqual(reconcile(data)['status'], 'no_alignment')


class ReconcileCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.source = Path(self.directory.name) / 'source.json'
        self.output = Path(self.directory.name) / 'report.json'
        self.source.write_text(json.dumps(acceptance()), encoding='utf-8')

    def run_cli(self, source=None, output=None, expect=0):
        source = str(self.source if source is None else source)
        output = str(self.output if output is None else output)
        env = dict(os.environ)
        env['PYTHONPATH'] = str(ROOT / 'src')
        completed = subprocess.run(
            [sys.executable, '-m', 'showdown_pots.reconcile_cli',
             '--source', source, '--output', output],
            cwd=str(ROOT), env=env, capture_output=True, text=True)
        self.assertEqual(
            completed.returncode, expect,
            msg=f'stderr={completed.stderr!r} stdout={completed.stdout!r}')
        return completed

    def test_success_summary_and_published_report(self):
        completed = self.run_cli()
        self.assertEqual(json.loads(completed.stdout),
                         {'max_pairs': 4, 'reconciled': 1, 'status': 'ok'})
        payload = json.loads(self.output.read_text(encoding='utf-8'))
        self.assertEqual(payload['offsets'], [{'from': 0, 'to': 0},
                                              {'from': 2, 'to': 2}])

    def test_no_alignment_is_published(self):
        source = Path(self.directory.name) / 'conflict.json'
        data = acceptance()
        data['anchors'] = [{'left_id': 'L3', 'right_id': 'R3'},
                           {'left_id': 'L4', 'right_id': 'R4'}]
        source.write_text(json.dumps(data), encoding='utf-8')
        completed = self.run_cli(source=source)
        self.assertEqual(json.loads(completed.stdout),
                         {'max_pairs': None, 'reconciled': 0,
                          'status': 'no_alignment'})
        payload = json.loads(self.output.read_text(encoding='utf-8'))
        self.assertEqual(payload['status'], 'no_alignment')
        self.assertEqual(payload['left'], [])

    def test_existing_output_is_never_overwritten(self):
        self.output.write_text('{"keep": true}', encoding='utf-8')
        self.run_cli(expect=2)
        self.assertEqual(self.output.read_text(encoding='utf-8'),
                         '{"keep": true}')

    def test_existing_symlink_output_is_never_followed(self):
        target = Path(self.directory.name) / 'target.json'
        target.write_text('{"keep": true}', encoding='utf-8')
        self.output.symlink_to(target)
        self.run_cli(expect=2)
        self.assertEqual(target.read_text(encoding='utf-8'), '{"keep": true}')

    def test_invalid_input_publishes_nothing(self):
        bad = Path(self.directory.name) / 'bad.json'
        data = acceptance()
        del data['clock']
        bad.write_text(json.dumps(data), encoding='utf-8')
        completed = self.run_cli(source=bad, expect=2)
        self.assertEqual(completed.stdout, '')
        self.assertIn('error:', completed.stderr)
        self.assertFalse(self.output.exists())

    def test_duplicate_json_key_is_exit_two(self):
        bad = Path(self.directory.name) / 'duplicate.json'
        bad.write_text('{"left": [], "left": [], "right": [], '
                       '"clock": {"min_offset": 0, "max_offset": 0, '
                       '"tolerance": 0}, "anchors": []}', encoding='utf-8')
        completed = self.run_cli(source=bad, expect=2)
        self.assertEqual(completed.stdout, '')
        self.assertFalse(self.output.exists())

    def test_missing_source_is_exit_two(self):
        completed = self.run_cli(
            source=Path(self.directory.name) / 'missing.json', expect=2)
        self.assertEqual(completed.stdout, '')
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
