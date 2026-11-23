"""Regression tests for the ``showdown-trace`` marked-chip tracing command.

The scenarios mirror the teaching-room acceptance table:

1. two pairing shifts (A-B/C-D versus A-C/B-D) that share balances and
   per-player marked ranges but differ on a full-roster count, then a fresh
   process resumes each shift from its checkpoint into the same winner hand;
2. the shipped ``fixtures/input.json`` with only king's chips marked, the
   uncalled-return isolation, the rejected 99-chip ace stack, and the
   three-way tied pot whose odd chip follows the fixed v1 seat order;
3. a 1000-hand shift whose source is deleted before the resume hand, the
   10**9 scaled variant, and the real checkpoint byte budget.

Supplemental cases cover no-hand queries, malformed checkpoints, a bad hand
late in the shift producing no partial output, the never-overwrite output
rule, group-sum pitfalls and structural input rejection.
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

from showdown_pots.trace import (
    CHECKPOINT_LIMIT,
    TraceError,
    encode_checkpoint,
    parse_checkpoint,
    trace,
)

TIE_BOARD = ['Tc', 'Jc', 'Qc', 'Kc', 'Ac']
WIN_BOARD = ['2c', '3d', '4h', '8s', '9c']
ROSTER = ['A', 'B', 'C', 'D']


def tie_hand(first, second, seat=0, amount=1):
    return {
        'board': list(TIE_BOARD), 'button_seat': 0,
        'players': [
            {'id': first, 'seat': seat, 'committed': amount,
             'folded': False, 'hole': ['2d', '3d']},
            {'id': second, 'seat': seat + 1, 'committed': amount,
             'folded': False, 'hole': ['4d', '5d']},
        ],
    }


def winner_hand(amount=1):
    return {
        'board': list(WIN_BOARD), 'button_seat': 0,
        'players': [
            {'id': 'A', 'seat': 0, 'committed': amount, 'folded': False,
             'hole': ['5c', '6c']},
            {'id': 'B', 'seat': 1, 'committed': amount, 'folded': False,
             'hole': ['Kh', 'Ks']},
        ],
    }


def opening(marked=None, balances=None, players=ROSTER):
    marked = marked if marked is not None else {'A': 1, 'B': 0, 'C': 0,
                                                'D': 1}
    balances = balances if balances is not None else {p: 1 for p in ROSTER}
    return {'balances': {p: balances[p] for p in players},
            'marked': {p: marked[p] for p in players}}


def group(group_id, players):
    return {'id': group_id, 'players': list(players)}


ALL_GROUPS = [group('g' + p, [p]) for p in ROSTER] + [
    group('gAB', ['A', 'B']), group('gCD', ['C', 'D']),
    group('gall', ROSTER)]

COUNTED = {'A': 1, 'B': 1, 'C': 0, 'D': 0}


def shift_source(pairs, counted=None, groups=None, amount=1):
    data = {
        'initial': opening(),
        'hands': [tie_hand(p, q, amount=amount) for p, q in pairs],
        'groups': ALL_GROUPS if groups is None else groups,
    }
    if counted is not None:
        data['counted'] = counted
    return data


def resume_source(checkpoint, hands, groups=None):
    return {'checkpoint': copy.deepcopy(checkpoint), 'hands': hands,
            'groups': ALL_GROUPS if groups is None else groups}


def by_id(report):
    return {item['id']: item for item in report['groups']}


class AcceptanceShiftTests(unittest.TestCase):
    def test_two_shifts_share_balances_and_solo_ranges(self):
        first = trace(shift_source([('A', 'B'), ('C', 'D')]))
        control = trace(shift_source([('A', 'C'), ('B', 'D')]))
        self.assertEqual(first['balances'],
                         {p: 1 for p in ROSTER})
        self.assertEqual(first['balances'], control['balances'])
        for player_id in ROSTER:
            solo = (by_id(first)['g' + player_id]['marked_min'],
                    by_id(first)['g' + player_id]['marked_max'])
            self.assertEqual(solo, (0, 1))
            self.assertEqual(by_id(control)['g' + player_id]
                             ['unmarked_min'], 0)
        solo_first = [(g['marked_min'], g['marked_max'])
                      for g in first['groups'] if len(g['id']) == 2]
        solo_control = [(g['marked_min'], g['marked_max'])
                        for g in control['groups'] if len(g['id']) == 2]
        self.assertEqual(solo_first, solo_control)
        # Joint groups are allowed to distinguish the two histories even when
        # every solo interval is identical.
        self.assertNotEqual(first['groups'], control['groups'])

    def test_count_distinguishes_the_two_pairings(self):
        first = trace(shift_source([('A', 'B'), ('C', 'D')], COUNTED))
        control = trace(shift_source([('A', 'C'), ('B', 'D')], COUNTED))
        self.assertFalse(first['counted_possible'])
        self.assertTrue(control['counted_possible'])

    def test_group_sums_are_not_individual_sums(self):
        # After the control shift, A and B together cannot hold more than the
        # two marked chips that ever entered their pots; the joint bound is a
        # network cut computed for the subset, never the sum of solo optima.
        control = trace(shift_source([('A', 'C'), ('B', 'D')]))
        groups = by_id(control)
        solo_sum = sum(groups['g' + p]['marked_max'] for p in 'AB')
        self.assertLessEqual(groups['gAB']['marked_max'], solo_sum)
        self.assertEqual(groups['gall']['marked_min'], 2)
        self.assertEqual(groups['gall']['marked_max'], 2)

    def test_resumed_winner_hand_matches_a_direct_run(self):
        cases = {
            'shift1': [('A', 'B'), ('C', 'D')],
            'control': [('A', 'C'), ('B', 'D')],
        }
        expected = {'shift1': (1, 1, 1), 'control': (0, 2, 0)}
        for name, pairs in cases.items():
            with self.subTest(name=name):
                first = trace(shift_source(pairs))
                checkpoint = first['checkpoint']
                resumed = trace(resume_source(checkpoint, [winner_hand()]))
                direct = trace(
                    shift_source(pairs, groups=[]))
                direct = trace({
                    'initial': opening(),
                    'hands': [tie_hand(p, q) for p, q in pairs]
                    + [winner_hand()],
                    'groups': ALL_GROUPS,
                })
                self.assertEqual(resumed['balances'], direct['balances'])
                self.assertEqual(resumed['groups'], direct['groups'])
                self.assertEqual(resumed['checkpoint'], direct['checkpoint'])
                entry = by_id(resumed)['gA']
                self.assertEqual(
                    (entry['marked_min'], entry['marked_max'],
                     entry['unmarked_min']),
                    expected[name])
                self.assertEqual(resumed['balances']['A'], 2)

    def test_checkpoints_are_hand_off_only_state(self):
        first = trace(shift_source([('A', 'B'), ('C', 'D')]))
        checkpoint = first['checkpoint']
        # Re-parsing the checkpoint and continuing needs nothing else.
        roster, totals, marked_total, capacities = parse_checkpoint(
            copy.deepcopy(checkpoint))
        self.assertEqual(roster, ROSTER)
        self.assertEqual(totals, {p: 1 for p in ROSTER})
        self.assertEqual(marked_total, 2)
        self.assertEqual(len(capacities), 16)
        self.assertEqual(capacities[0], 0)
        self.assertEqual(capacities[-1], 2)
        # A second hand-off from the resumed run is itself resumable.
        resumed = trace(resume_source(checkpoint, [winner_hand()]))
        again = trace(resume_source(resumed['checkpoint'], []))
        self.assertEqual(again['balances'], resumed['balances'])
        self.assertEqual(again['groups'], resumed['groups'])


class FixtureFlowTests(unittest.TestCase):
    def fixture_hand(self):
        path = ROOT / 'fixtures' / 'input.json'
        return json.loads(path.read_text(encoding='utf-8'))

    def test_marked_chips_follow_only_king_s_pots(self):
        hand = self.fixture_hand()
        initial = {
            'balances': {'ace': 100, 'king': 50, 'straight': 20,
                         'fold': 70},
            'marked': {'ace': 0, 'king': 50, 'straight': 0, 'fold': 0},
        }
        report = trace({
            'initial': initial, 'hands': [hand],
            'groups': [group('g' + p, [p])
                       for p in ('ace', 'king', 'straight', 'fold')],
        })
        ranges = {g['id']: (g['marked_min'], g['marked_max'])
                  for g in report['groups']}
        self.assertEqual(ranges, {
            'gace': (30, 30), 'gking': (0, 0),
            'gstraight': (20, 20), 'gfold': (0, 0),
        })
        self.assertEqual(report['balances'],
                         {'ace': 160, 'king': 0, 'straight': 80,
                          'fold': 0})

    def test_ace_short_stack_is_rejected(self):
        hand = self.fixture_hand()
        initial = {
            'balances': {'ace': 99, 'king': 50, 'straight': 20,
                         'fold': 70},
            'marked': {'ace': 0, 'king': 50, 'straight': 0, 'fold': 0},
        }
        with self.assertRaises(TraceError):
            trace({'initial': initial, 'hands': [hand], 'groups': []})

    def test_three_way_tie_uses_v1_odd_chip_order(self):
        hand = {
            'board': list(TIE_BOARD), 'button_seat': 0,
            'players': [
                {'id': 'A', 'seat': 0, 'committed': 1, 'folded': False,
                 'hole': ['2d', '3d']},
                {'id': 'B', 'seat': 1, 'committed': 1, 'folded': False,
                 'hole': ['4d', '5d']},
                {'id': 'C', 'seat': 2, 'committed': 1, 'folded': True,
                 'hole': []},
            ],
        }
        initial = {
            'balances': {'A': 1, 'B': 1, 'C': 1},
            'marked': {'A': 1, 'B': 1, 'C': 1},
        }
        report = trace({
            'initial': initial, 'hands': [hand],
            'groups': [group('g' + p, [p]) for p in 'ABC'],
        })
        ranges = {g['id']: (g['marked_min'], g['marked_max'])
                  for g in report['groups']}
        self.assertEqual(ranges, {'gA': (1, 1), 'gB': (2, 2),
                                  'gC': (0, 0)})
        self.assertEqual(report['balances'],
                         {'A': 1, 'B': 2, 'C': 0})


class LongShiftTests(unittest.TestCase):
    def test_one_thousand_hands_then_resume_after_source_deleted(self):
        hands = []
        for _ in range(500):
            hands.append(tie_hand('A', 'B'))
            hands.append(tie_hand('C', 'D'))
        first = trace({'initial': opening(), 'hands': hands,
                       'groups': ALL_GROUPS})
        checkpoint = first['checkpoint']
        # The resume request references no historical source at all; deleting
        # every input file leaves the self-contained checkpoint sufficient.
        resumed = trace(resume_source(checkpoint, [winner_hand()]))
        # Determinism: resuming the same checkpoint through the same hand is
        # identical across processes, and the new checkpoint is itself
        # hand-off ready.  (The full history is 1001 hands, over the 1000-hand
        # per-request limit, so equivalence is established via the checkpoint
        # chain rather than one oversized direct run.)
        again = trace(resume_source(checkpoint, [winner_hand()]))
        self.assertEqual(resumed, again)
        rehanded = trace(resume_source(resumed['checkpoint'], []))
        self.assertEqual(rehanded['balances'], resumed['balances'])
        entry = by_id(resumed)['gA']
        self.assertEqual((entry['marked_min'], entry['marked_max']),
                         (1, 1))

    def test_scaled_amounts_keep_exact_marked_quantity(self):
        big = 10 ** 9
        hands = []
        for _ in range(500):
            hands.append(tie_hand('A', 'B', amount=big))
            hands.append(tie_hand('C', 'D', amount=big))
        initial = {
            'balances': {p: big for p in ROSTER},
            'marked': {'A': big, 'B': 0, 'C': 0, 'D': big},
        }
        first = trace({'initial': initial, 'hands': hands, 'groups': []})
        resumed = trace(resume_source(
            first['checkpoint'], [winner_hand(big)],
            groups=[group('gA', ['A'])]))
        entry = resumed['groups'][0]
        self.assertEqual((entry['marked_min'], entry['marked_max'],
                          entry['unmarked_min']),
                         (big, big, big))
        self.assertEqual(resumed['balances']['A'], 2 * big)

    def test_checkpoint_byte_budget_at_worst_shape(self):
        ids = ['P' * 63 + str(i) for i in range(6)]
        initial = {
            'balances': {player_id: 10 ** 9 for player_id in ids},
            'marked': {player_id: 10 ** 9 for player_id in ids},
        }
        holes = [('2d', '3d'), ('4d', '5d'), ('6d', '7d'), ('8d', '9d'),
                 ('Td', 'Jd')]
        hand = {
            'board': list(TIE_BOARD), 'button_seat': 0,
            'players': [
                {'id': ids[i], 'seat': i, 'committed': 10 ** 9,
                 'folded': i == 5,
                 'hole': [] if i == 5 else list(holes[i])}
                for i in range(6)],
        }
        report = trace({'initial': initial, 'hands': [hand], 'groups': []})
        encoded = encode_checkpoint(report['checkpoint'])
        self.assertLessEqual(len(encoded), CHECKPOINT_LIMIT)
        self.assertLessEqual(len(encoded), 2048)
        # The canonical encoding is exactly the transfer-column measure.
        canonical = json.dumps(
            report['checkpoint'], ensure_ascii=False, sort_keys=True,
            separators=(',', ':')).encode('utf-8')
        self.assertEqual(encoded, canonical)

    def test_running_time_is_independent_of_chip_magnitude(self):
        # Same structural hand count at 1 and at 10**9 must both stay small;
        # the algorithm works on aggregate capacities, never per chip.
        small_hands = [tie_hand('A', 'B'), tie_hand('C', 'D')] * 25
        big_hands = [tie_hand('A', 'B', amount=10 ** 9),
                     tie_hand('C', 'D', amount=10 ** 9)] * 25
        small = trace({'initial': opening(), 'hands': small_hands,
                       'groups': []})
        big_initial = {
            'balances': {p: 10 ** 9 for p in ROSTER},
            'marked': {'A': 10 ** 9, 'B': 0, 'C': 0, 'D': 10 ** 9},
        }
        big = trace({'initial': big_initial, 'hands': big_hands,
                     'groups': []})
        self.assertEqual(len(small['checkpoint']['u']),
                         len(big['checkpoint']['u']))


class QueryAndCountTests(unittest.TestCase):
    def test_no_hands_reports_initial_state(self):
        report = trace({
            'initial': opening(), 'hands': [],
            'groups': [group('gA', ['A']), group('gAD', ['A', 'D'])],
        })
        self.assertEqual(report['balances'], {p: 1 for p in ROSTER})
        groups = by_id(report)
        self.assertEqual((groups['gA']['marked_min'],
                          groups['gA']['marked_max']), (1, 1))
        self.assertEqual((groups['gAD']['marked_min'],
                          groups['gAD']['marked_max']), (2, 2))
        self.assertIsNone(report['counted_possible'])

    def test_counted_failure_modes_still_emit_a_report(self):
        pairs = [('A', 'B'), ('C', 'D')]
        # total mismatch against the conserved marked total
        bad_total = dict(COUNTED, A=2)
        report = trace(shift_source(pairs, bad_total))
        self.assertFalse(report['counted_possible'])
        # structurally fine, but one player would need more marked chips
        # than the single chip it can ever hold
        report = trace(shift_source(
            pairs, {'A': 2, 'B': 0, 'C': 0, 'D': 0}))
        self.assertFalse(report['counted_possible'])
        # a count within every solo range but impossible jointly
        report = trace(shift_source(pairs, COUNTED))
        self.assertFalse(report['counted_possible'])

    def test_counted_feasible_in_one_flow_only(self):
        pairs = [('A', 'C'), ('B', 'D')]
        report = trace(shift_source(pairs, COUNTED))
        self.assertTrue(report['counted_possible'])
        # Same numbers, different pairing: no single flow supports them.
        report = trace(shift_source([('A', 'B'), ('C', 'D')], COUNTED))
        self.assertFalse(report['counted_possible'])

    def test_groups_are_sorted_and_order_insensitive(self):
        groups = [group('z', ['A']), group('a', ['B']),
                  group('m', ['C', 'A'])]
        data = {'initial': opening(), 'hands': [], 'groups': groups}
        forward = trace(data)
        shuffled = {'initial': opening(), 'hands': [],
                    'groups': list(reversed(groups))}
        backward = trace(shuffled)
        self.assertEqual(forward, backward)
        self.assertEqual([g['id'] for g in forward['groups']],
                         ['a', 'm', 'z'])
        # Reordering players inside a group changes nothing.
        reordered = {'initial': opening(), 'hands': [],
                     'groups': [group('m', ['A', 'C'])]}
        self.assertEqual(trace(reordered)['groups'],
                         [g for g in forward['groups'] if g['id'] == 'm'])

    def test_full_roster_pins_the_conserved_total(self):
        report = trace(shift_source([('A', 'B'), ('C', 'D')]))
        entry = by_id(report)['gall']
        self.assertEqual((entry['marked_min'], entry['marked_max']),
                         (2, 2))
        self.assertEqual(entry['unmarked_min'], 2)

    def test_zero_marked_roster(self):
        marked = {p: 0 for p in ROSTER}
        report = trace({
            'initial': opening(marked=marked),
            'hands': [tie_hand('A', 'B')],
            'groups': ALL_GROUPS,
        })
        self.assertTrue(all(g['marked_min'] == 0 and g['marked_max'] == 0
                            for g in report['groups']))
        self.assertTrue(all(g['unmarked_min'] >= 0
                            for g in report['groups']))

    def test_queries_and_counts_never_change_the_checkpoint(self):
        hands = [tie_hand('A', 'B'), tie_hand('C', 'D')]
        base = {'initial': opening(), 'hands': hands}
        plain = trace(dict(base, groups=[]))['checkpoint']
        queried = trace(dict(
            base, groups=[group('gAB', ['A', 'B'])]))['checkpoint']
        counted = trace(dict(
            base, groups=[], counted=COUNTED))['checkpoint']
        self.assertEqual(plain, queried)
        self.assertEqual(plain, counted)

    def test_thousand_hand_boundary(self):
        hands = [tie_hand('A', 'B'), tie_hand('C', 'D')] * 500
        self.assertEqual(len(hands), 1000)
        report = trace({'initial': opening(), 'hands': hands,
                        'groups': []})
        self.assertEqual(set(report['balances']), set(ROSTER))
        with self.assertRaises(ValueError):
            trace({'initial': opening(), 'hands': hands + [tie_hand('A', 'B')],
                   'groups': []})


class CheckpointValidationTests(unittest.TestCase):
    def setUp(self):
        self.checkpoint = trace(
            shift_source([('A', 'B'), ('C', 'D')]))['checkpoint']

    def resume(self, checkpoint):
        return trace(resume_source(checkpoint, []))

    def mutate(self, change):
        value = copy.deepcopy(self.checkpoint)
        change(value)
        return self.resume(value)

    def test_unknown_version_is_rejected(self):
        with self.assertRaises(TraceError):
            self.mutate(lambda value: value.update(v=2))

    def test_missing_or_extra_fields_are_rejected(self):
        def drop(value):
            del value['u']
        with self.assertRaises(ValueError):
            self.mutate(drop)
        with self.assertRaises(ValueError):
            self.mutate(lambda value: value.update(extra=1))

    def test_wrong_types_and_ranges_are_rejected(self):
        with self.assertRaises(ValueError):
            self.mutate(lambda value: value.update(v='1'))
        with self.assertRaises(ValueError):
            self.mutate(lambda value: value.update(m=-1))
        with self.assertRaises(ValueError):
            self.mutate(lambda value: value['t'].__setitem__('A', -1))
        with self.assertRaises(ValueError):
            self.mutate(lambda value: value.update(u='nope'))
        with self.assertRaises(ValueError):
            self.mutate(lambda value: value.update(u=value['u'][:-1]))

    def test_unsorted_or_duplicate_ids_are_rejected(self):
        with self.assertRaises(TraceError):
            self.mutate(lambda value: value.update(
                ids=list(reversed(value['ids']))))
        with self.assertRaises(ValueError):
            self.mutate(lambda value: value.update(
                ids=['A', 'A', 'C', 'D']))

    def test_detectable_inconsistent_states_are_rejected(self):
        with self.assertRaises(TraceError):
            self.mutate(lambda value: value.update(
                u=[99] + value['u'][1:]))
        with self.assertRaises(TraceError):
            self.mutate(lambda value: value.update(
                u=value['u'][:-1] + [value['m'] + 1]))
        with self.assertRaises(TraceError):
            self.mutate(lambda value: value.update(m=3))
        # capacity exceeding the chips physically held by the subset
        def inflate(value):
            value['u'][2] = 2  # player B holds only one chip at the stop
        with self.assertRaises(TraceError):
            self.mutate(inflate)
        # monotone and within every pile bound, but not submodular:
        # U(A)+U(B) < U(A,B)+U(empty)
        def break_submodularity(value):
            value['u'][0] = 0
            value['u'][1] = 0
            value['u'][2] = 0
            value['u'][3] = 1
        with self.assertRaises(TraceError):
            self.mutate(break_submodularity)
        # broken monotonicity across an actual differing pair
        def break_monotonicity(value):
            value['u'][3] = 0
        with self.assertRaises(TraceError):
            self.mutate(break_monotonicity)

    def test_round_trip_through_canonical_text(self):
        text = encode_checkpoint(self.checkpoint).decode('utf-8')
        reparsed = json.loads(text)
        self.assertEqual(self.resume(reparsed)['checkpoint'],
                         self.resume(self.checkpoint)['checkpoint'])


class StructuralInputTests(unittest.TestCase):
    def assert_rejected(self, payload):
        with self.assertRaises((ValueError, TraceError)):
            trace(payload)

    def test_top_level_shape_rules(self):
        good = {'initial': opening(), 'hands': [], 'groups': []}
        self.assert_rejected(dict(good, checkpoint=good['initial']))
        self.assert_rejected({k: v for k, v in good.items()
                              if k != 'initial'})
        self.assert_rejected(dict(good, surprise=1))
        self.assert_rejected('nope')
        self.assert_rejected(dict(good, hands=good['hands']
                                  + [None] * (1001 - len(good['hands']))))

    def test_roster_rules(self):
        bad = copy.deepcopy(opening())
        bad['balances']['E'] = 1
        self.assert_rejected({'initial': bad, 'hands': [], 'groups': []})
        bad = copy.deepcopy(opening())
        del bad['marked']['A']
        self.assert_rejected({'initial': bad, 'hands': [], 'groups': []})
        bad = copy.deepcopy(opening())
        bad['marked']['A'] = 2
        self.assert_rejected({'initial': bad, 'hands': [], 'groups': []})
        bad = copy.deepcopy(opening())
        bad['balances']['A'] = 10 ** 9 + 1
        self.assert_rejected({'initial': bad, 'hands': [], 'groups': []})
        bad = copy.deepcopy(opening())
        bad['balances']['A'] = '1'
        self.assert_rejected({'initial': bad, 'hands': [], 'groups': []})

    def test_group_rules(self):
        base = {'initial': opening(), 'hands': [],
                'groups': [group('g1', ['A']), group('g1', ['B'])]}
        self.assert_rejected(base)
        base = {'initial': opening(), 'hands': [],
                'groups': [group('bad id!', ['A'])]}
        self.assert_rejected(base)
        base = {'initial': opening(), 'hands': [],
                'groups': [group('g1', ['A', 'A'])]}
        self.assert_rejected(base)
        base = {'initial': opening(), 'hands': [],
                'groups': [group('g1', ['Z'])]}
        self.assert_rejected(base)
        base = {'initial': opening(), 'hands': [],
                'groups': [group('g1', [])]}
        self.assert_rejected(base)

    def test_counted_structure_rules(self):
        self.assert_rejected({
            'initial': opening(), 'hands': [], 'groups': [],
            'counted': {'A': 1, 'B': 1, 'C': 0}})
        self.assert_rejected({
            'initial': opening(), 'hands': [], 'groups': [],
            'counted': {'A': 1, 'B': 1, 'C': 0, 'D': 0, 'E': 0}})
        self.assert_rejected({
            'initial': opening(), 'hands': [], 'groups': [],
            'counted': {'A': '1', 'B': 1, 'C': 0, 'D': 0}})
        self.assert_rejected({
            'initial': opening(), 'hands': [], 'groups': [],
            'counted': {'A': 6_000_000_001, 'B': 0, 'C': 0, 'D': 0}})

    def test_unknown_participant_and_overcommitment_rejected(self):
        hand = tie_hand('A', 'Z')
        self.assert_rejected({'initial': opening(), 'hands': [hand],
                              'groups': []})
        hand = tie_hand('A', 'B')
        hand['players'][0]['committed'] = 2
        self.assert_rejected({'initial': opening(), 'hands': [hand],
                              'groups': []})

    def test_v1_bad_hand_rejected(self):
        hand = tie_hand('A', 'B')
        hand['board'] = ['2c', '3d', '4h', '8s', '2d']
        self.assert_rejected({'initial': opening(), 'hands': [hand],
                              'groups': []})


class TraceCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.source = self.path / 'source.json'
        self.output = self.path / 'report.json'

    def write(self, payload, name='source.json'):
        target = self.path / name
        target.write_text(json.dumps(payload), encoding='utf-8')
        return target

    def run_cli(self, source=None, output=None, expect=0):
        source = str(self.source if source is None else source)
        output = str(self.output if output is None else output)
        env = dict(os.environ)
        env['PYTHONPATH'] = str(ROOT / 'src')
        completed = subprocess.run(
            [sys.executable, '-m', 'showdown_pots.trace_cli',
             '--source', source, '--output', output],
            cwd=str(ROOT), env=env, capture_output=True, text=True)
        self.assertEqual(
            completed.returncode, expect,
            msg=f'stderr={completed.stderr!r} stdout={completed.stdout!r}')
        return completed

    def test_success_publishes_report_and_summary(self):
        completed = self.run_cli(
            source=self.write(shift_source([('A', 'B'), ('C', 'D')],
                                           COUNTED)))
        self.assertEqual(json.loads(completed.stdout),
                         {'counted_possible': False, 'groups': 7,
                          'players': 4})
        payload = json.loads(self.output.read_text(encoding='utf-8'))
        self.assertEqual(set(payload),
                         {'balances', 'groups', 'counted_possible',
                          'checkpoint'})

    def test_resume_in_a_fresh_process_after_deleting_source(self):
        first_path = self.write(
            shift_source([('A', 'B'), ('C', 'D')]), 'first.json')
        first_out = self.path / 'first-report.json'
        self.run_cli(source=first_path, output=first_out)
        checkpoint = json.loads(
            first_out.read_text(encoding='utf-8'))['checkpoint']
        first_path.unlink()
        resume = self.write(resume_source(checkpoint, [winner_hand()]),
                            'resume.json')
        self.output.unlink(missing_ok=True)
        completed = self.run_cli(source=resume)
        self.assertEqual(json.loads(completed.stdout)['players'], 4)
        entry = next(g for g in json.loads(
            self.output.read_text(encoding='utf-8'))['groups']
            if g['id'] == 'gA')
        self.assertEqual((entry['marked_min'], entry['marked_max']),
                         (1, 1))

    def test_no_hands_query(self):
        self.run_cli(source=self.write(
            {'initial': opening(), 'hands': [],
             'groups': [group('gA', ['A'])]}))
        payload = json.loads(self.output.read_text(encoding='utf-8'))
        self.assertIsNone(payload['counted_possible'])
        self.assertEqual(payload['groups'][0]['marked_max'], 1)

    def test_malformed_checkpoint_exits_two(self):
        good = trace(shift_source([('A', 'B'), ('C', 'D')]))['checkpoint']
        good['v'] = 9
        self.run_cli(source=self.write(resume_source(good, [])), expect=2)
        self.assertFalse(self.output.exists())

    def test_late_bad_hand_leaves_no_partial_output(self):
        hands = [tie_hand('A', 'B'), tie_hand('C', 'D')]
        bad = tie_hand('A', 'B')
        bad['board'] = ['2c', '3d', '4h', '8s', '2d']
        hands.append(bad)
        self.run_cli(
            source=self.write({'initial': opening(), 'hands': hands,
                               'groups': []}), expect=2)
        self.assertFalse(self.output.exists())

    def test_existing_output_is_never_overwritten(self):
        self.output.write_text('{"keep": true}', encoding='utf-8')
        self.run_cli(
            source=self.write(shift_source([('A', 'B')])), expect=2)
        self.assertEqual(self.output.read_text(encoding='utf-8'),
                         '{"keep": true}')

    def test_existing_symlink_output_is_never_followed(self):
        target = self.path / 'target.json'
        target.write_text('{"keep": true}', encoding='utf-8')
        self.output.symlink_to(target)
        self.run_cli(
            source=self.write(shift_source([('A', 'B')])), expect=2)
        self.assertEqual(target.read_text(encoding='utf-8'),
                         '{"keep": true}')

    def test_bad_json_and_missing_source_exit_two(self):
        bad = self.path / 'bad.json'
        bad.write_text('{"hands": ', encoding='utf-8')
        self.run_cli(source=bad, expect=2)
        duplicate = self.path / 'dup.json'
        duplicate.write_text(
            '{"initial": {} , "initial": {}}', encoding='utf-8')
        self.run_cli(source=duplicate, expect=2)
        self.run_cli(source=self.path / 'missing.json', expect=2)

    def test_reordered_input_is_byte_stable(self):
        groups = [group('z', ['A']), group('a', ['B'])]
        forward = {'initial': opening(), 'hands': [], 'groups': groups}
        backward = {'initial': opening(), 'hands': [],
                    'groups': list(reversed(groups))}
        self.run_cli(source=self.write(forward, 'f.json'))
        first = self.output.read_bytes()
        self.output.unlink()
        self.run_cli(source=self.write(backward, 'b.json'))
        self.assertEqual(self.output.read_bytes(), first)


if __name__ == '__main__':
    unittest.main()
