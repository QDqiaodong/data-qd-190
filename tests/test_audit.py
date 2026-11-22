"""Regression tests for the occluded-hand ``showdown-audit`` command.

The acceptance hand comes from docs/audit.md: four players each commit one
chip, the only pot is decided by which unknown card the live player A ends up
holding.  The same solver is additionally checked for certification parity
with the v1 single-hand command, input-error handling, plan soundness and
bounded run time at the twelve-slot limit.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(ROOT / 'src'))

from showdown_pots import audit as audit_module
from showdown_pots.audit import AuditError, _AuditModel, _parse_request, audit
from showdown_pots.service import process

BOARD = ['2c', '3d', '4h', '8s', '9c']


def slot(slot_id, candidates):
    return {'slot_id': slot_id, 'candidates': list(candidates)}


def base_hand():
    """The four-player acceptance hand with slots L, s, a, b."""
    return {
        'board': list(BOARD),
        'button_seat': 0,
        'players': [
            {'id': 'A', 'seat': 0, 'committed': 1, 'folded': False,
             'hole': ['5c', slot('L', ['Kd', 'Ac', '6c', 'Qd'])]},
            {'id': 'B', 'seat': 1, 'committed': 1, 'folded': False,
             'hole': ['Kh', 'Ks']},
            {'id': 'F1', 'seat': 2, 'committed': 1, 'folded': True,
             'hole': [slot('s', ['Kd', 'Ac']), slot('a', ['Kd', '6c'])]},
            {'id': 'F2', 'seat': 3, 'committed': 1, 'folded': True,
             'hole': [slot('b', ['Ac', 'Qd']), '7h']},
        ],
    }


def base_request(budget):
    return {'hand': base_hand(), 'inspectable': ['s', 'a', 'b'], 'budget': budget}


def extended_hand():
    hand = base_hand()
    hand['players'].append(
        {'id': 'C', 'seat': 4, 'committed': 3, 'folded': False,
         'hole': ['8c', 'Jh']})
    hand['players'].append(
        {'id': 'D', 'seat': 5, 'committed': 2, 'folded': False,
         'hole': ['2d', 'Ts']})
    return hand


EXPECTED_BUDGET2_PLAN = {
    'slot_id': 's',
    'branches': {
        'Kd': {
            'slot_id': 'b',
            'branches': {
                'Ac': {'awards': [{'pot': 1, 'awards': {'B': 4}}]},
                'Qd': {'awards': [{'pot': 1, 'awards': {'A': 4}}]},
            },
        },
        'Ac': {
            'slot_id': 'a',
            'branches': {
                'Kd': {'awards': [{'pot': 1, 'awards': {'A': 4}}]},
                '6c': {'awards': [{'pot': 1, 'awards': {'B': 4}}]},
            },
        },
    },
}


class AuditCoreTests(unittest.TestCase):
    def test_base_scenario_budgets(self):
        for budget in (0, 1):
            result = audit(base_request(budget))
            self.assertEqual(result['status'], 'ambiguous')
            self.assertIsNone(result['plan'])
            self.assertEqual(len(result['witnesses']), 2)
            pot = result['pots'][0]
            self.assertEqual(
                pot,
                {'pot': 1, 'from_exclusive': 0, 'to_inclusive': 1,
                 'amount': 4, 'status': 'ambiguous', 'awards': None},
            )
            self.assertEqual(result['uncalled_returns'], [])

        result = audit(base_request(2))
        self.assertEqual(result['status'], 'ambiguous')
        self.assertEqual(result['plan'], EXPECTED_BUDGET2_PLAN)

    def test_witnesses_are_complete_feasible_inputs_with_diverging_reports(self):
        result = audit(base_request(2))
        first, second = result['witnesses']
        # Each witness source settles under the unchanged v1 command.
        reports = [process(first['source']), process(second['source'])]
        self.assertEqual(reports[0], first['report'])
        self.assertEqual(reports[1], second['report'])
        self.assertNotEqual(
            reports[0]['pots'][0]['awards'], reports[1]['pots'][0]['awards'])
        winners = {tuple(sorted(report['pots'][0]['awards'])) for report in reports}
        self.assertEqual(winners, {('A',), ('B',)})
        # No card is repeated inside either completed physical deck.
        for source in (first['source'], second['source']):
            cards = list(source['board'])
            for player in source['players']:
                cards.extend(player['hole'])
            self.assertEqual(len(cards), len(set(cards)))

    def test_fixed_slot_removal_keeps_two_branches_and_budget_one_suffices(self):
        hand = base_hand()
        hand['players'][2]['hole'][0] = 'Kd'  # s fixed to Kd
        result = audit({'hand': hand, 'inspectable': ['a', 'b'], 'budget': 1})
        self.assertEqual(result['status'], 'ambiguous')
        self.assertEqual(result['plan'], {
            'slot_id': 'b',
            'branches': {
                'Ac': {'awards': [{'pot': 1, 'awards': {'B': 4}}]},
                'Qd': {'awards': [{'pot': 1, 'awards': {'A': 4}}]},
            },
        })
        # Both branches still correspond to feasible completions.
        model = _AuditModel(_parse_request(
            {'hand': hand, 'inspectable': ['a', 'b'], 'budget': 1}))
        for card_value in ('Ac', 'Qd'):
            state = frozenset(
                (other, tuple(c for c in choices if c != card_value))
                for other, choices in model.initial_state if other != 'b')
            self.assertIsNotNone(model.aggregate(state, (('b', card_value),)))

    def test_no_fixed_pair_of_looks_guarantees_certification(self):
        """Any pre-committed pair leaves at least one ambiguous observation."""
        model = _AuditModel(_parse_request(base_request(2)))
        for first_slot in ('s', 'a', 'b'):
            for second_slot in ('s', 'a', 'b'):
                if first_slot >= second_slot:
                    continue
                ambiguous_leaf = False
                for first_card, second_state, second_prefix in self._pairs(
                        model, first_slot, second_slot):
                    summary = model.aggregate(second_state, second_prefix)
                    if summary is not None and summary['differing']:
                        ambiguous_leaf = True
                        break
                self.assertTrue(
                    ambiguous_leaf,
                    msg=f'fixed pair {first_slot},{second_slot} wrongly resolves everything',
                )

    @staticmethod
    def _pairs(model, first_slot, second_slot):
        for first_card in dict(model.initial_state)[first_slot]:
            state_one = frozenset(
                (other, tuple(c for c in choices if c != first_card))
                for other, choices in model.initial_state if other != first_slot)
            prefix_one = ((first_slot, first_card),)
            if model.aggregate(state_one, prefix_one) is None:
                continue
            for second_card in dict(state_one)[second_slot]:
                state_two = frozenset(
                    (other, tuple(c for c in choices if c != second_card))
                    for other, choices in state_one if other != second_slot)
                yield first_card, state_two, tuple(sorted(
                    prefix_one + ((second_slot, second_card),)))

    def test_extended_hand_side_pot_certified_and_uncalled_return(self):
        request = {'hand': extended_hand(),
                   'inspectable': ['s', 'a', 'b'], 'budget': 2}
        result = audit(request)
        self.assertEqual(result['status'], 'ambiguous')
        main_pot, side_pot = result['pots']
        self.assertEqual(main_pot['amount'], 6)
        self.assertEqual(main_pot['status'], 'ambiguous')
        self.assertIsNone(main_pot['awards'])
        self.assertEqual(side_pot, {
            'pot': 2, 'from_exclusive': 1, 'to_inclusive': 2, 'amount': 2,
            'status': 'certified', 'awards': {'C': 2}})
        self.assertEqual(result['uncalled_returns'], [
            {'player_id': 'C', 'from_exclusive': 2, 'to_inclusive': 3, 'amount': 1}])
        # Every leaf must carry both pots once the main pot is resolved.
        leaves = []
        self._collect_leaves(result['plan'], {}, leaves)
        self.assertTrue(leaves)
        for awards in leaves:
            self.assertEqual([item['pot'] for item in awards], [1, 2])
            self.assertEqual(
                next(item for item in awards if item['pot'] == 2)['awards'],
                {'C': 2})
        self.assertEqual(audit({**request, 'budget': 1})['plan'], None)

    @staticmethod
    def _collect_leaves(node, observations, leaves):
        if 'awards' in node:
            leaves.append(node['awards'])
            return
        for card_value, child in node['branches'].items():
            AuditCoreTests._collect_leaves(
                child, {**observations, node['slot_id']: card_value}, leaves)

    def test_impossible_candidate_is_not_a_plan_branch(self):
        hand = {
            'board': list(BOARD), 'button_seat': 0,
            'players': [
                {'id': 'A', 'seat': 0, 'committed': 1, 'folded': False,
                 'hole': ['5c', slot('X', ['6c', 'Ac', 'Kd', '2c'])]},
                {'id': 'B', 'seat': 1, 'committed': 1, 'folded': False,
                 'hole': ['Kh', 'Ks']},
            ],
        }
        result = audit({'hand': hand, 'inspectable': ['X'], 'budget': 1})
        self.assertEqual(set(result['plan']['branches']), {'6c', 'Ac', 'Kd'})
        model = _AuditModel(_parse_request(
            {'hand': hand, 'inspectable': ['X'], 'budget': 1}))
        for card_value in result['plan']['branches']:
            state = frozenset(
                (other, tuple(c for c in choices if c != card_value))
                for other, choices in model.initial_state if other != 'X')
            self.assertIsNotNone(model.aggregate(state, (('X', card_value),)))

    def test_three_slots_sharing_two_cards_is_inconsistent(self):
        hand = {
            'board': list(BOARD), 'button_seat': 0,
            'players': [
                {'id': 'A', 'seat': 0, 'committed': 1, 'folded': False,
                 'hole': [slot('x', ['Kd', 'Ac']), '5c']},
                {'id': 'B', 'seat': 1, 'committed': 1, 'folded': False,
                 'hole': ['Kh', 'Ks']},
                {'id': 'F', 'seat': 2, 'committed': 1, 'folded': True,
                 'hole': [slot('y', ['Kd', 'Ac']), slot('z', ['Kd', 'Ac'])]},
            ],
        }
        self.assertEqual(audit(
            {'hand': hand, 'inspectable': ['x', 'y', 'z'], 'budget': 2}),
            {'status': 'inconsistent', 'pots': [], 'uncalled_returns': [],
             'witnesses': [], 'plan': None})

    def test_duplicate_fixed_cards_is_inconsistent_not_input_error(self):
        hand = {
            'board': list(BOARD), 'button_seat': 0,
            'players': [
                {'id': 'A', 'seat': 0, 'committed': 1, 'folded': False,
                 'hole': ['5c', '6c']},
                {'id': 'B', 'seat': 1, 'committed': 1, 'folded': False,
                 'hole': ['Kh', '5c']},
            ],
        }
        result = audit({'hand': hand, 'inspectable': [], 'budget': 0})
        self.assertEqual(result['status'], 'inconsistent')
        self.assertEqual(result['pots'], [])

    def test_certified_fixed_hand_matches_v1_including_odd_chip(self):
        fixed = {
            'board': ['Tc', 'Jc', 'Qc', 'Kc', 'Ac'], 'button_seat': 1,
            'players': [
                {'id': 'straight', 'seat': 0, 'committed': 1,
                 'folded': False, 'hole': ['5c', '6c']},
                {'id': 'fold', 'seat': 1, 'committed': 1,
                 'folded': True, 'hole': []},
                {'id': 'ace', 'seat': 2, 'committed': 1,
                 'folded': False, 'hole': ['Ah', '2d']},
            ],
        }
        v1 = process(fixed)
        result = audit({'hand': fixed, 'inspectable': [], 'budget': 0})
        self.assertEqual(result['status'], 'certified')
        self.assertEqual(result['witnesses'], [])
        self.assertEqual(
            result['pots'][0]['awards'], v1['pots'][0]['awards'])
        self.assertEqual(result['plan'], {
            'awards': [{'pot': 1, 'awards': v1['pots'][0]['awards']}]})

    def test_certified_hand_without_matched_pots(self):
        hand = {
            'board': list(BOARD), 'button_seat': 0,
            'players': [
                {'id': 'A', 'seat': 0, 'committed': 3, 'folded': False,
                 'hole': ['5c', '6c']},
                {'id': 'B', 'seat': 1, 'committed': 1, 'folded': True,
                 'hole': []},
            ],
        }
        result = audit({'hand': hand, 'inspectable': [], 'budget': 0})
        self.assertEqual(result['status'], 'certified')
        # Level 1 is a matched pot won by the only live player; level 3 is a
        # single-contributor layer returned uncalled.
        self.assertEqual(result['pots'], [{
            'pot': 1, 'from_exclusive': 0, 'to_inclusive': 1, 'amount': 2,
            'status': 'certified', 'awards': {'A': 2}}])
        self.assertEqual(result['uncalled_returns'], [
            {'player_id': 'A', 'from_exclusive': 1, 'to_inclusive': 3,
             'amount': 2}])
        self.assertEqual(result['plan'], {
            'awards': [{'pot': 1, 'awards': {'A': 2}}]})

    def test_certified_when_only_uncalled_returns_exist(self):
        # One contributor funds the whole hand: no matched pots at all.
        hand = {
            'board': list(BOARD), 'button_seat': 0,
            'players': [
                {'id': 'A', 'seat': 0, 'committed': 4, 'folded': False,
                 'hole': ['5c', '6c']},
                {'id': 'B', 'seat': 1, 'committed': 0, 'folded': True,
                 'hole': []},
            ],
        }
        result = audit({'hand': hand, 'inspectable': [], 'budget': 0})
        self.assertEqual(result['status'], 'certified')
        self.assertEqual(result['pots'], [])
        self.assertEqual(result['uncalled_returns'], [
            {'player_id': 'A', 'from_exclusive': 0, 'to_inclusive': 4,
             'amount': 4}])
        self.assertEqual(result['plan'], {'awards': []})

    def test_plan_soundness_on_every_node(self):
        """Every branch stays feasible, no slot repeats, leaves are certain."""
        for request in (base_request(2),
                        {'hand': extended_hand(),
                         'inspectable': ['s', 'a', 'b'], 'budget': 2}):
            model = _AuditModel(_parse_request(request))
            plan = model.run()['plan']
            self._assert_node(model, model.initial_state, (), plan, set())

    def _assert_node(self, model, state, prefix, node, seen):
        summary = model.aggregate(state, prefix)
        self.assertIsNotNone(summary)
        if 'awards' in node:
            self.assertEqual(summary['differing'], set())
            expected = [
                {'pot': pot['pot'], 'awards': dict(pot['awards'])}
                for pot in summary['template_pots']]
            self.assertEqual(node['awards'], expected)
            self.assertLessEqual(len(seen), model.budget)
            return
        slot_id = node['slot_id']
        self.assertNotIn(slot_id, seen)
        self.assertIn(slot_id, model.inspectable)
        feasible_labels = {
            card_value for card_value in dict(state)[slot_id]
            if model.aggregate(frozenset(
                (other, tuple(c for c in choices if c != card_value))
                for other, choices in state if other != slot_id),
                tuple(sorted(prefix + ((slot_id, card_value),)))) is not None
        }
        self.assertEqual(set(node['branches']), feasible_labels)
        for card_value, child in node['branches'].items():
            next_state = frozenset(
                (other, tuple(c for c in choices if c != card_value))
                for other, choices in state if other != slot_id)
            next_prefix = tuple(sorted(prefix + ((slot_id, card_value),)))
            self._assert_node(
                model, next_state, next_prefix, child, seen | {slot_id})

    def test_report_is_stable_across_runs(self):
        first = json.dumps(audit(base_request(2)), sort_keys=True)
        second = json.dumps(audit(base_request(2)), sort_keys=True)
        self.assertEqual(first, second)

    def test_malformed_requests_exit_as_input_errors(self):
        good = base_request(1)

        def mutate(**changes):
            data = json.loads(json.dumps(good))
            data.update(changes)
            return data

        cases = [
            {},
            mutate(budget=3),
            mutate(budget=True),
            {'hand': good['hand'], 'inspectable': 's', 'budget': 1},
            {'hand': good['hand'], 'inspectable': ['s', 's'], 'budget': 1},
            {'hand': good['hand'], 'inspectable': ['nope'], 'budget': 1},
            mutate(hand={**good['hand'], 'button_seat': 9}),
            mutate(hand={**good['hand'], 'players': [
                {**good['hand']['players'][0], 'id': 'B'}]}),
        ]
        # Duplicate slot ids and duplicate/over-long candidate lists.
        dup_slot = json.loads(json.dumps(good))
        dup_slot['hand']['players'][0]['hole'][1]['slot_id'] = 's'
        cases.append(dup_slot)
        bad_candidates = json.loads(json.dumps(good))
        bad_candidates['hand']['players'][0]['hole'][1]['candidates'].append('2c')
        cases.append(bad_candidates)
        dup_candidate = json.loads(json.dumps(good))
        dup_candidate['hand']['players'][2]['hole'][0]['candidates'] = ['Kd', 'Kd']
        cases.append(dup_candidate)
        # Matched layer with no eligible live player is an input error.
        dead_layer = {
            'board': list(BOARD), 'button_seat': 0,
            'players': [
                {'id': 'A', 'seat': 0, 'committed': 2, 'folded': True,
                 'hole': []},
                {'id': 'B', 'seat': 1, 'committed': 2, 'folded': True,
                 'hole': []},
                {'id': 'C', 'seat': 2, 'committed': 1, 'folded': False,
                 'hole': ['Kh', 'Ks']},
            ],
        }
        cases.append({'hand': dead_layer, 'inspectable': [], 'budget': 0})
        for data in cases:
            with self.assertRaises(ValueError):
                audit(data)

    def test_at_most_twelve_slots(self):
        thirteen = {
            'board': [slot(f'b{i}', ['2c', '3d']) for i in range(5)],
            'button_seat': 0,
            'players': [
                {'id': 'A', 'seat': 0, 'committed': 1, 'folded': False,
                 'hole': [slot('h0', ['4h', '5h']), slot('h1', ['6h', '7h'])]},
                {'id': 'B', 'seat': 1, 'committed': 1, 'folded': False,
                 'hole': [slot('h2', ['8h', '9h']), slot('h3', ['Th', 'Jh'])]},
                {'id': 'C', 'seat': 2, 'committed': 1, 'folded': False,
                 'hole': [slot('h4', ['Qh', 'Kh']), slot('h5', ['Ah', '2h'])]},
                {'id': 'D', 'seat': 3, 'committed': 1, 'folded': False,
                 'hole': [slot('h6', ['3h', '4s']), slot('h7', ['5s', '6s'])]},
            ],
        }
        with self.assertRaises(AuditError):
            audit({'hand': thirteen, 'inspectable': [], 'budget': 0})
        twelve = json.loads(json.dumps(thirteen))
        twelve['board'][0] = '2c'  # twelve slots remain, structurally allowed
        result = audit({'hand': twelve, 'inspectable': [], 'budget': 0})
        self.assertIn(result['status'], ('certified', 'ambiguous', 'inconsistent'))


class AuditPerformanceTests(unittest.TestCase):
    def test_independent_tier_avoids_joint_enumeration(self):
        # Ten relevant slots over disjoint candidate pools: a naive walk
        # would visit 4**10 completions; the symbolic tier stays instant.
        all_cards = [r + s for r in '23456789TJQKA' for s in 'cdhs']
        fixed = ['2c', '3d', '4h', '8s', '9c', '5c', '6c', 'Kh', 'Ks', '7h', '2d']
        remaining = [c for c in all_cards if c not in fixed]
        pools = [remaining[4 * i:4 * (i + 1)] for i in range(10)]
        hand = {
            'board': ['2c', '3d', '4h', '8s', '9c'], 'button_seat': 0,
            'players': [
                {'id': 'A', 'seat': 0, 'committed': 1, 'folded': False,
                 'hole': ['5c', '6c']},
                {'id': 'B', 'seat': 1, 'committed': 1, 'folded': False,
                 'hole': ['Kh', 'Ks']},
            ] + [
                {'id': f'P{i + 2}', 'seat': i + 2, 'committed': 1,
                 'folded': False,
                 'hole': [slot(f's{2 * i}', pools[2 * i]),
                          slot(f's{2 * i + 1}', pools[2 * i + 1])]}
                for i in range(5)
            ],
        }
        started = time.monotonic()
        result = audit({'hand': hand, 'inspectable': [], 'budget': 0})
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertEqual(result['status'], 'ambiguous')

    def test_twelve_slot_limit_stays_offline_bounded(self):
        # Twelve disjoint pools that still fit one physical deck.
        all_cards = [r + s for r in '23456789TJQKA' for s in 'cdhs']
        fixed = ['2c', '3d', '4h', '8s', '9c']
        remaining = [c for c in all_cards if c not in fixed]
        pools = [remaining[4 * i:4 * (i + 1)] for i in range(11)] \
            + [remaining[44:47]]
        hand = {
            'board': fixed, 'button_seat': 0,
            'players': [
                {'id': chr(ord('A') + i), 'seat': i, 'committed': 1,
                 'folded': False,
                 'hole': [slot(f'h{2 * i}', pools[2 * i]),
                          slot(f'h{2 * i + 1}', pools[2 * i + 1])]}
                for i in range(6)
            ],
        }
        started = time.monotonic()
        result = audit({'hand': hand, 'inspectable': [], 'budget': 0})
        self.assertLess(time.monotonic() - started, 30.0)
        self.assertIn(result['status'], ('certified', 'ambiguous'))


class AuditCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.source = Path(self.directory.name) / 'source.json'
        self.output = Path(self.directory.name) / 'report.json'
        self.source.write_text(json.dumps(base_request(2)), encoding='utf-8')

    def run_cli(self, source=None, output=None, expect=0):
        source = str(self.source if source is None else source)
        output = str(self.output if output is None else output)
        env = dict(os.environ)
        env['PYTHONPATH'] = str(ROOT / 'src')
        completed = subprocess.run(
            [sys.executable, '-m', 'showdown_pots.audit_cli',
             '--source', source, '--output', output],
            cwd=str(ROOT), env=env, capture_output=True, text=True)
        self.assertEqual(
            completed.returncode, expect,
            msg=f'stderr={completed.stderr!r} stdout={completed.stdout!r}')
        return completed

    def test_success_writes_report_and_summary(self):
        completed = self.run_cli()
        self.assertEqual(
            json.loads(completed.stdout), {'pots': 1, 'status': 'ambiguous'})
        payload = json.loads(self.output.read_text(encoding='utf-8'))
        self.assertEqual(payload['plan'], EXPECTED_BUDGET2_PLAN)
        self.assertEqual(len(payload['witnesses']), 2)

    def test_existing_output_is_never_overwritten(self):
        self.output.write_text('{"keep": true}', encoding='utf-8')
        self.run_cli(expect=2)
        self.assertEqual(self.output.read_text(encoding='utf-8'), '{"keep": true}')

    def test_invalid_input_publishes_nothing(self):
        bad = Path(self.directory.name) / 'bad.json'
        bad.write_text(json.dumps({'hand': {}, 'inspectable': [], 'budget': 0}),
                       encoding='utf-8')
        completed = self.run_cli(source=bad, expect=2)
        self.assertEqual(completed.stdout, '')
        self.assertIn('error:', completed.stderr)
        self.assertFalse(self.output.exists())

    def test_missing_source_is_exit_two(self):
        completed = self.run_cli(
            source=Path(self.directory.name) / 'missing.json', expect=2)
        self.assertEqual(completed.stdout, '')
        self.assertFalse(self.output.exists())

    def test_inconsistent_input_is_a_published_result(self):
        hand = {
            'board': list(BOARD), 'button_seat': 0,
            'players': [
                {'id': 'A', 'seat': 0, 'committed': 1, 'folded': False,
                 'hole': [slot('x', ['Kd', 'Ac']), '5c']},
                {'id': 'B', 'seat': 1, 'committed': 1, 'folded': False,
                 'hole': ['Kh', 'Ks']},
                {'id': 'F', 'seat': 2, 'committed': 1, 'folded': True,
                 'hole': [slot('y', ['Kd', 'Ac']), slot('z', ['Kd', 'Ac'])]},
            ],
        }
        source = Path(self.directory.name) / 'inconsistent.json'
        source.write_text(json.dumps(
            {'hand': hand, 'inspectable': ['x', 'y', 'z'], 'budget': 2}),
            encoding='utf-8')
        completed = self.run_cli(source=source)
        self.assertEqual(
            json.loads(completed.stdout), {'pots': 0, 'status': 'inconsistent'})
        payload = json.loads(self.output.read_text(encoding='utf-8'))
        self.assertEqual(payload['status'], 'inconsistent')
        self.assertIsNone(payload['plan'])


if __name__ == '__main__':
    unittest.main()
