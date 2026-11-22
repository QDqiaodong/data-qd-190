"""Regression tests for the ``showdown-replay`` betting-street replay command.

The scenarios mirror the teaching-group acceptance table: a five-handed flop
with two cumulative short all-ins, a checkpoint that is handed to a second
process after its source file is gone, independent student branches, and a
finished hand settled by the unchanged v1 rules.  Supplemental cases cover
short-stack calls, hands that end early, lone funded players, seat wrap, the
checkpoint byte budget at the maximum table size, and the CLI failure
boundary.
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

from showdown_pots.replay import ReplayError, replay
from showdown_pots.service import process

BOARD = ['2c', '3d', '4h', '8s', '9c']


def player(player_id, seat, stack, hole):
    return {'player_id': player_id, 'seat': seat, 'stack': stack,
            'hole': list(hole)}


def opening_players(except_e=None):
    stacks = {'A': 101, 'B': 101, 'C': 15, 'D': 101, 'E': 21}
    if except_e is not None:
        stacks['E'] = except_e
    holes = {'A': ['5c', '6c'], 'B': ['Kh', 'Ks'], 'C': ['9d', '9h'],
             'D': ['Jc', 'Jd'], 'E': ['8c', '8h']}
    seats = {'A': 1, 'B': 2, 'C': 3, 'D': 4, 'E': 5}
    return [player(name, seats[name], stacks[name], holes[name])
            for name in 'ABCDE']


def opening(e_stack=None, e_target=20):
    """A to 10, B calls, C all-in 14, D calls, E all-in 20."""
    return {
        'start': {'button_seat': 0, 'board': list(BOARD), 'ante': 1,
                  'min_bet': 10, 'players': opening_players(e_stack)},
        'actions': [
            {'player_id': 'A', 'kind': 'raise_to', 'to': 10},
            {'player_id': 'B', 'kind': 'call'},
            {'player_id': 'C', 'kind': 'raise_to', 'to': 14},
            {'player_id': 'D', 'kind': 'call'},
            {'player_id': 'E', 'kind': 'raise_to', 'to': e_target},
        ],
    }


def action(player_id, kind, to=None):
    value = {'player_id': player_id, 'kind': kind}
    if to is not None:
        value['to'] = to
    return value


def branch(branch_id, actions):
    return {'id': branch_id, 'actions': actions}


def branches(checkpoint, items):
    return {'checkpoint': copy.deepcopy(checkpoint), 'branches': items}


def checkpoint_of(data):
    return replay(data)['checkpoint']


def settlement_of(result):
    return {item['id']: item for item in result['branches']}


def nets(report):
    return {p['player_id']: p['net'] for p in report['players']}


NORMAL_ACTIONS = [
    action('A', 'call'), action('B', 'call'), action('D', 'call'),
    action('A', 'check'), action('B', 'check'), action('D', 'check'),
    action('A', 'check'), action('B', 'check'), action('D', 'check'),
]

RAISE_ACTIONS = [
    action('A', 'raise_to', 30), action('B', 'call'), action('D', 'fold'),
    action('A', 'check'), action('B', 'check'),
    action('A', 'check'), action('B', 'check'),
]


class AcceptanceRuleTests(unittest.TestCase):
    def test_cumulative_short_all_ins_restore_a_full_raise(self):
        result = replay(opening())
        self.assertIsNone(result['settlement'])
        self.assertEqual(result['next'], {
            'player_id': 'A', 'street': 'flop', 'to_call': 10,
            'call_chips': 10, 'can_check': False,
            'raise_to_min': 30, 'raise_to_max': 100,
            'short_all_in_to': None,
        })

    def test_short_all_in_below_threshold_denies_a_raise(self):
        result = replay(opening(e_stack=20, e_target=19))
        self.assertEqual(result['next']['to_call'], 9)
        self.assertFalse(result['next']['can_check'])
        self.assertIsNone(result['next']['raise_to_min'])
        self.assertIsNone(result['next']['raise_to_max'])
        self.assertIsNone(result['next']['short_all_in_to'])

    def test_acted_players_regain_the_raise_only_after_a_full_raise(self):
        # A and B call 20: D owes 6 and last faced 14, so cannot raise.
        data = opening()
        data['actions'] += [action('A', 'call'), action('B', 'call')]
        result = replay(data)
        self.assertEqual(result['next']['player_id'], 'D')
        self.assertEqual(result['next']['to_call'], 6)
        self.assertIsNone(result['next']['raise_to_min'])
        # A raises to 30 and B calls: D owes 16 and may raise to 40.
        data = opening()
        data['actions'] += [action('A', 'raise_to', 30), action('B', 'call')]
        result = replay(data)
        self.assertEqual(result['next']['player_id'], 'D')
        self.assertEqual(result['next']['to_call'], 16)
        self.assertEqual(result['next']['raise_to_min'], 40)
        self.assertEqual(result['next']['raise_to_max'], 100)

    def test_checkpoint_round_trips_through_a_fresh_parse(self):
        checkpoint = checkpoint_of(opening())
        resumed = replay(branches(checkpoint, [branch('a', [])]))
        self.assertEqual(resumed['branches'][0]['status'], 'ok')
        self.assertEqual(resumed['branches'][0]['checkpoint'], checkpoint)
        self.assertEqual(resumed['branches'][0]['next']['raise_to_min'], 30)

    def test_short_all_in_offer_when_a_full_raise_is_impossible(self):
        # After the flop, B's whole remaining stack is a legal short raise.
        data = {
            'start': {'button_seat': 0, 'board': list(BOARD), 'ante': 1,
                      'min_bet': 10,
                      'players': [player('A', 1, 50, ['5c', '6c']),
                                  player('B', 2, 13, ['Kh', 'Ks'])]},
            'actions': [action('A', 'raise_to', 10)],
        }
        result = replay(data)
        # B has 12 chips behind and owes 10; a full raise target is 20 but
        # the most B can reach is 12, which is still above the current price.
        self.assertEqual(result['next']['to_call'], 10)
        self.assertEqual(result['next']['call_chips'], 10)
        self.assertIsNone(result['next']['raise_to_min'])
        self.assertIsNone(result['next']['raise_to_max'])
        self.assertEqual(result['next']['short_all_in_to'], 12)


class AcceptanceBranchTests(unittest.TestCase):
    def setUp(self):
        self.checkpoint = checkpoint_of(opening())

    def test_normal_branch_settles_through_v1(self):
        result = replay(branches(self.checkpoint,
                                 [branch('normal', NORMAL_ACTIONS)]))
        report = result['branches'][0]['settlement']
        self.assertEqual([pot['amount'] for pot in report['pots']], [75, 24])
        self.assertEqual(nets(report), {'A': 78, 'B': -21, 'C': -15,
                                        'D': -21, 'E': -21})
        self.assertIsNone(result['branches'][0]['next'])

    def test_raise_branch_settles_through_v1(self):
        result = replay(branches(self.checkpoint,
                                 [branch('raise', RAISE_ACTIONS)]))
        report = result['branches'][0]['settlement']
        self.assertEqual([pot['amount'] for pot in report['pots']],
                         [75, 18, 20])
        self.assertEqual(nets(report)['A'], 82)
        self.assertEqual(nets(report)['D'], -15)
        self.assertIsNone(result['branches'][0]['next'])

    def test_illegal_raise_reports_the_third_action(self):
        result = replay(branches(self.checkpoint, [branch('bad-raise', [
            action('A', 'call'), action('B', 'call'),
            action('D', 'raise_to', 30)])]))
        item = result['branches'][0]
        self.assertEqual(item['status'], 'illegal')
        self.assertEqual(item['action_index'], 3)
        self.assertTrue(item['reason'])
        self.assertNotIn('checkpoint', item)
        self.assertNotIn('settlement', item)

    def test_acting_out_of_turn_reports_the_fifth_action(self):
        result = replay(branches(self.checkpoint, [branch('bad-order', [
            action('A', 'call'), action('B', 'call'), action('D', 'call'),
            action('A', 'check'), action('A', 'check')])]))
        item = result['branches'][0]
        self.assertEqual(item['status'], 'illegal')
        self.assertEqual(item['action_index'], 5)

    def test_failures_do_not_affect_successful_siblings(self):
        result = replay(branches(self.checkpoint, [
            branch('normal', NORMAL_ACTIONS),
            branch('bad-raise', [action('A', 'call'), action('B', 'call'),
                                 action('D', 'raise_to', 30)]),
            branch('raise', RAISE_ACTIONS),
        ]))
        by_id = settlement_of(result)
        self.assertEqual(by_id['normal']['status'], 'ok')
        self.assertEqual(by_id['raise']['status'], 'ok')
        self.assertEqual(by_id['bad-raise']['status'], 'illegal')
        self.assertEqual(nets(by_id['normal']['settlement'])['A'], 78)
        self.assertEqual(nets(by_id['raise']['settlement'])['A'], 82)

    def test_branch_order_and_ids_are_deterministic(self):
        items = [branch('student-b', RAISE_ACTIONS),
                 branch('student-a', NORMAL_ACTIONS)]
        forward = replay(branches(self.checkpoint, items))
        backward = replay(branches(self.checkpoint, list(reversed(items))))
        self.assertEqual(forward, backward)
        self.assertEqual([item['id'] for item in forward['branches']],
                         ['student-a', 'student-b'])

    def test_successful_midpoint_hands_off_to_a_new_process(self):
        full = replay(branches(self.checkpoint,
                               [branch('raise', RAISE_ACTIONS)]))
        midpoint = replay(branches(self.checkpoint, [branch('part', [
            action('A', 'raise_to', 30), action('B', 'call')])]))
        stop = midpoint['branches'][0]['checkpoint']
        rest = replay(branches(stop, [branch('rest', [
            action('D', 'fold'), action('A', 'check'), action('B', 'check'),
            action('A', 'check'), action('B', 'check')])]))
        self.assertEqual(rest['branches'][0]['settlement'],
                         full['branches'][0]['settlement'])

    def test_from_opening_matches_from_checkpoint(self):
        direct = replay({
            'start': opening()['start'],
            'actions': opening()['actions'] + RAISE_ACTIONS,
        })
        resumed = replay(branches(self.checkpoint,
                                  [branch('raise', RAISE_ACTIONS)]))
        self.assertEqual(direct['settlement'],
                         resumed['branches'][0]['settlement'])


class SupplementalRuleTests(unittest.TestCase):
    def test_short_stack_call_pays_only_the_remaining_chips(self):
        data = {
            'start': {'button_seat': 0, 'board': list(BOARD), 'ante': 1,
                      'min_bet': 10,
                      'players': [player('A', 1, 101, ['5c', '6c']),
                                  player('B', 2, 11, ['Kh', 'Ks'])]},
            'actions': [action('A', 'raise_to', 30)],
        }
        result = replay(data)
        # B started with 10 chips after the ante and owes 30.
        self.assertEqual(result['next']['to_call'], 30)
        self.assertEqual(result['next']['call_chips'], 10)
        self.assertIsNone(result['next']['raise_to_min'])
        self.assertIsNone(result['next']['short_all_in_to'])
        finished = replay(branches(result['checkpoint'],
                                   [branch('call', [action('B', 'call')])]))
        report = finished['branches'][0]['settlement']
        self.assertIsNotNone(report)
        self.assertEqual({p['player_id']: p['committed']
                          for p in report['players']}, {'A': 31, 'B': 11})
        self.assertEqual(report['summary']['net_sum'], 0)

    def test_hand_ends_when_only_one_player_remains(self):
        data = {
            'start': {'button_seat': 0, 'board': list(BOARD), 'ante': 1,
                      'min_bet': 10,
                      'players': [player('A', 1, 101, ['5c', '6c']),
                                  player('B', 2, 101, ['Kh', 'Ks']),
                                  player('C', 3, 101, ['9d', '9h'])]},
            'actions': [action('A', 'raise_to', 10), action('B', 'fold'),
                        action('C', 'fold')],
        }
        result = replay(data)
        self.assertIsNone(result['next'])
        report = result['settlement']
        self.assertEqual(report['summary']['total_committed'], 13)
        self.assertEqual(nets(report), {'A': 2, 'B': -1, 'C': -1})
        self.assertEqual(report['uncalled_returns'],
                         [{'player_id': 'A', 'from_exclusive': 1,
                           'to_inclusive': 11, 'amount': 10}])

    def test_lone_funded_player_may_only_call_or_fold(self):
        data = {
            'start': {'button_seat': 0, 'board': list(BOARD), 'ante': 1,
                      'min_bet': 1,
                      'players': [player('A', 1, 3, ['5c', '6c']),
                                  player('B', 2, 101, ['Kh', 'Ks'])]},
            'actions': [action('A', 'raise_to', 2)],
        }
        result = replay(data)
        self.assertEqual(result['next']['player_id'], 'B')
        self.assertEqual(result['next']['to_call'], 2)
        self.assertFalse(result['next']['can_check'])
        self.assertIsNone(result['next']['raise_to_min'])
        self.assertIsNone(result['next']['short_all_in_to'])
        checks = replay(branches(result['checkpoint'], [
            branch('check', [action('B', 'check')]),
            branch('raise', [action('B', 'raise_to', 10)]),
            branch('call', [action('B', 'call')]),
            branch('fold', [action('B', 'fold')]),
        ]))
        by_id = settlement_of(checks)
        self.assertEqual(by_id['check']['action_index'], 1)
        self.assertEqual(by_id['raise']['action_index'], 1)
        self.assertEqual(by_id['call']['status'], 'ok')
        self.assertEqual(by_id['fold']['status'], 'ok')

    def test_button_and_seats_wrap_clockwise(self):
        # Button on seat 8; seats 1, 2 and 7 are occupied.  Action starts at
        # seat 1 (after 8, wrapping through 0) and later wraps from 2 to 7.
        data = {
            'start': {'button_seat': 8, 'board': list(BOARD), 'ante': 1,
                      'min_bet': 1,
                      'players': [player('A', 1, 50, ['5c', '6c']),
                                  player('B', 7, 50, ['Kh', 'Ks']),
                                  player('C', 2, 50, ['9d', '9h'])]},
            'actions': [],
        }
        self.assertEqual(replay(data)['next']['player_id'], 'A')
        data['actions'] = [action('A', 'check')]
        self.assertEqual(replay(data)['next']['player_id'], 'C')
        data['actions'] = [action('A', 'check'), action('C', 'check')]
        self.assertEqual(replay(data)['next']['player_id'], 'B')
        data['actions'] = [action('A', 'check'), action('C', 'check'),
                           action('B', 'check')]
        self.assertEqual(replay(data)['next']['street'], 'turn')
        self.assertEqual(replay(data)['next']['player_id'], 'A')

    def test_empty_street_is_skipped_when_nobody_can_act(self):
        # A is all-in after the flop; B still has chips but owes nothing.
        # The turn and river are dealt with no betting.
        data = {
            'start': {'button_seat': 0, 'board': list(BOARD), 'ante': 1,
                      'min_bet': 1,
                      'players': [player('A', 1, 3, ['5c', '6c']),
                                  player('B', 2, 101, ['Kh', 'Ks'])]},
            'actions': [action('A', 'raise_to', 2), action('B', 'call')],
        }
        result = replay(data)
        self.assertIsNone(result['next'])
        self.assertIsNotNone(result['settlement'])
        self.assertEqual(result['checkpoint']['street'], 2)

    def test_settlement_matches_the_v1_command(self):
        result = replay(opening())
        final = replay(branches(result['checkpoint'],
                                [branch('normal', NORMAL_ACTIONS)]))
        report = final['branches'][0]['settlement']
        committed = {p['player_id']: p['committed']
                     for p in report['players']}
        folded = {p['player_id']: False for p in report['players']}
        expected = process({
            'board': list(BOARD), 'button_seat': 0,
            'players': [
                {'id': name, 'seat': {'A': 1, 'B': 2, 'C': 3, 'D': 4,
                                      'E': 5}[name],
                 'committed': committed[name], 'folded': folded[name],
                 'hole': {'A': ['5c', '6c'], 'B': ['Kh', 'Ks'],
                          'C': ['9d', '9h'], 'D': ['Jc', 'Jd'],
                          'E': ['8c', '8h']}[name]}
                for name in 'ABCDE'
            ],
        })
        self.assertEqual(report, expected)

    def test_tied_board_odd_chip_and_uncalled_return_match_v1(self):
        # Board plays for both live players; the lowest pot is odd, and the
        # extra chip goes clockwise from the button.  A's last raise is also
        # unmatched and returns to him through v1.
        data = {
            'start': {'button_seat': 0, 'board': ['As', 'Ks', 'Qs', 'Js',
                                                  'Ts'],
                      'ante': 1, 'min_bet': 1,
                      'players': [player('A', 1, 7, ['2c', '3c']),
                                  player('B', 2, 7, ['2d', '3d']),
                                  player('C', 3, 2, ['4c', '5c'])]},
            'actions': [action('A', 'raise_to', 3), action('B', 'call'),
                        action('C', 'fold'),
                        action('A', 'check'), action('B', 'check'),
                        action('A', 'check'), action('B', 'check')],
        }
        result = replay(data)
        report = result['settlement']
        self.assertEqual([pot['amount'] for pot in report['pots']], [3, 6])
        self.assertEqual(report['pots'][0]['winner_ids_in_odd_chip_order'],
                         ['A', 'B'])
        self.assertEqual(report['pots'][0]['awards'], {'A': 2, 'B': 1})
        self.assertEqual(nets(report), {'A': 1, 'B': 0, 'C': -1})
        self.assertEqual(nets(result['settlement']),
                         {p['player_id']: p['net']
                          for p in process({
                              'board': ['As', 'Ks', 'Qs', 'Js', 'Ts'],
                              'button_seat': 0,
                              'players': [
                                  {'id': 'A', 'seat': 1, 'committed': 4,
                                   'folded': False, 'hole': ['2c', '3c']},
                                  {'id': 'B', 'seat': 2, 'committed': 4,
                                   'folded': False, 'hole': ['2d', '3d']},
                                  {'id': 'C', 'seat': 3, 'committed': 1,
                                   'folded': True, 'hole': ['4c', '5c']},
                              ],
                          })['players']})

    def test_short_all_in_raise_keeps_min_full_raise_for_actors(self):
        # B short-shoves 12 over A's 10; C (yet to act) may make a full raise
        # to 22, but A already acted at 10 and has not faced a full raise, so
        # A may only call or fold.
        data = {
            'start': {'button_seat': 0, 'board': list(BOARD), 'ante': 1,
                      'min_bet': 10,
                      'players': [player('A', 1, 101, ['5c', '6c']),
                                  player('B', 2, 13, ['Kh', 'Ks']),
                                  player('C', 3, 101, ['9d', '9h'])]},
            'actions': [action('A', 'raise_to', 10)],
        }
        first = replay(data)
        self.assertEqual(first['next']['short_all_in_to'], 12)
        shoved = replay(branches(first['checkpoint'], [
            branch('shove', [action('B', 'raise_to', 12)])]))
        entry = shoved['branches'][0]
        self.assertEqual(entry['status'], 'ok')
        self.assertEqual(entry['next']['player_id'], 'C')
        self.assertEqual(entry['next']['to_call'], 12)
        self.assertEqual(entry['next']['raise_to_min'], 22)
        after = replay(branches(entry['checkpoint'], [
            branch('reraise', [action('C', 'call'), action('A', 'raise_to',
                                                          50)])]))
        self.assertEqual(after['branches'][0]['status'], 'illegal')
        self.assertEqual(after['branches'][0]['action_index'], 2)

    def test_terminal_checkpoint_accepts_empty_actions_only(self):
        data = {
            'start': {'button_seat': 0, 'board': list(BOARD), 'ante': 1,
                      'min_bet': 10,
                      'players': [player('A', 1, 101, ['5c', '6c']),
                                  player('B', 2, 101, ['Kh', 'Ks'])]},
            'actions': [action('A', 'raise_to', 10), action('B', 'fold')],
        }
        finished = replay(data)
        self.assertIsNone(finished['next'])
        reread = replay(branches(finished['checkpoint'], [
            branch('empty', []),
            branch('late', [action('A', 'check')]),
        ]))
        by_id = settlement_of(reread)
        self.assertEqual(by_id['empty']['status'], 'ok')
        self.assertEqual(by_id['empty']['settlement'],
                         finished['settlement'])
        self.assertIsNone(by_id['empty']['next'])
        self.assertEqual(by_id['late']['status'], 'illegal')
        self.assertEqual(by_id['late']['action_index'], 1)

    def test_short_all_in_can_be_recorded_but_full_raise_cannot_be_faked(self):
        # A short all-in is legal when the player is facing a full raise.
        data = {
            'start': {'button_seat': 0, 'board': list(BOARD), 'ante': 1,
                      'min_bet': 10,
                      'players': [player('A', 1, 101, ['5c', '6c']),
                                  player('B', 2, 8, ['Kh', 'Ks'])]},
            'actions': [action('A', 'raise_to', 10)],
        }
        result = replay(data)
        # B has 7 behind, owes 9; a shove to 7 is below the price and illegal.
        self.assertIsNone(result['next']['short_all_in_to'])
        bad = replay(branches(result['checkpoint'], [
            branch('shove', [action('B', 'raise_to', 7)])]))
        self.assertEqual(bad['branches'][0]['status'], 'illegal')


class CheckpointValidationTests(unittest.TestCase):
    def setUp(self):
        self.checkpoint = checkpoint_of(opening())

    def mutated(self, change):
        value = copy.deepcopy(self.checkpoint)
        change(value)
        return replay(branches(value, [branch('x', [])]))

    def test_unknown_version_is_rejected(self):
        with self.assertRaises(ReplayError):
            self.mutated(lambda value: value.update(version=2))

    def test_missing_or_extra_fields_are_rejected(self):
        with self.assertRaises(ValueError):
            self.mutated(lambda value: value.pop('min_bet'))
        with self.assertRaises(ValueError):
            self.mutated(lambda value: value.update(extra=1))

    def test_wrong_types_are_rejected(self):
        with self.assertRaises(ValueError):
            self.mutated(lambda value: value.update(street='flop'))
        with self.assertRaises(ValueError):
            self.mutated(lambda value: value.update(done=1))
        with self.assertRaises(ValueError):
            self.mutated(lambda value: value['players'][0].update(seat=True))

    def test_inconsistent_state_is_rejected(self):
        with self.assertRaises(ReplayError):
            self.mutated(lambda value: value.update(next='nobody'))
        with self.assertRaises(ReplayError):
            self.mutated(lambda value: value.update(price=999))
        with self.assertRaises(ReplayError):
            self.mutated(lambda value: value.update(done=True))

    def test_duplicate_cards_are_rejected(self):
        def clash(value):
            value['players'][0]['hole'][0] = value['board'][0]
        with self.assertRaises(ValueError):
            self.mutated(clash)

    def test_checkpoint_fits_the_terminal_budget(self):
        # Nine players, longest legal IDs, maximal chip values.
        ranks = '23456789TJQKA'
        deck = [rank + suit for rank in ranks for suit in 'cdhs']
        board = ['As', 'Ks', 'Qs', 'Js', 'Ts']
        used = set(board)
        players = []
        holes = []
        for index in range(9):
            hole = [value for value in deck if value not in used][:2]
            used.update(hole)
            holes.append(hole)
        ids = ['P' * 63 + str(index) for index in range(9)]
        for index in range(9):
            players.append(player(ids[index], index, 10 ** 9, holes[index]))
        data = {
            'start': {'button_seat': 8, 'board': board, 'ante': 1,
                      'min_bet': 10 ** 9, 'players': players},
            'actions': [action(ids[0], 'raise_to', 10 ** 9 - 1)],
        }
        checkpoint = replay(data)['checkpoint']
        encoded = json.dumps(checkpoint, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'))
        self.assertLessEqual(len(encoded.encode('utf-8')), 4096)
        self.assertEqual(len(checkpoint['players']), 9)
        self.assertEqual(len(checkpoint['players'][0]['id']), 64)


class ReplayCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.source = self.path / 'source.json'
        self.output = self.path / 'report.json'

    def run_cli(self, source=None, output=None, expect=0, script=None):
        source = str(self.source if source is None else source)
        output = str(self.output if output is None else output)
        env = dict(os.environ)
        env['PYTHONPATH'] = str(ROOT / 'src')
        completed = subprocess.run(
            [sys.executable, '-m', 'showdown_pots.replay_cli',
             '--source', source, '--output', output],
            cwd=str(ROOT), env=env, capture_output=True, text=True)
        self.assertEqual(
            completed.returncode, expect,
            msg=f'stderr={completed.stderr!r} stdout={completed.stdout!r}')
        return completed

    def write(self, payload, name='source.json'):
        target = self.path / name
        target.write_text(json.dumps(payload), encoding='utf-8')
        return target

    def test_opening_writes_checkpoint_and_next(self):
        completed = self.run_cli(source=self.write(opening()))
        self.assertEqual(json.loads(completed.stdout),
                         {'settled': False, 'street': 'flop'})
        payload = json.loads(self.output.read_text(encoding='utf-8'))
        self.assertEqual(set(payload), {'checkpoint', 'next', 'settlement'})
        self.assertIsNone(payload['settlement'])
        self.assertEqual(payload['next']['raise_to_min'], 30)

    def test_checkpoint_survives_deleting_the_source_file(self):
        first = self.path / 'first.json'
        first.write_text(json.dumps(opening()), encoding='utf-8')
        first_output = self.path / 'first-report.json'
        self.run_cli(source=first, output=first_output)
        checkpoint = json.loads(
            first_output.read_text(encoding='utf-8'))['checkpoint']
        first.unlink()
        self.output.unlink(missing_ok=True)
        payload = branches(checkpoint, [branch('normal', NORMAL_ACTIONS)])
        completed = self.run_cli(source=self.write(payload, 'resume.json'))
        self.assertEqual(json.loads(completed.stdout),
                         {'branches': 1, 'ok': 1})
        report = json.loads(self.output.read_text(encoding='utf-8'))
        entry = report['branches'][0]
        self.assertEqual(entry['status'], 'ok')
        self.assertEqual(entry['settlement']['summary']['total_committed'],
                         99)

    def test_branch_reordering_is_byte_stable(self):
        checkpoint = checkpoint_of(opening())
        items = [branch('student-b', RAISE_ACTIONS),
                 branch('student-a', NORMAL_ACTIONS)]
        self.run_cli(source=self.write(branches(checkpoint, items),
                                       'forward.json'))
        forward = self.output.read_bytes()
        self.output.unlink()
        self.run_cli(source=self.write(branches(checkpoint,
                                                list(reversed(items))),
                                       'backward.json'))
        self.assertEqual(self.output.read_bytes(), forward)

    def test_existing_output_is_never_overwritten(self):
        self.output.write_text('{"keep": true}', encoding='utf-8')
        self.run_cli(source=self.write(opening()), expect=2)
        self.assertEqual(self.output.read_text(encoding='utf-8'),
                         '{"keep": true}')

    def test_existing_symlink_output_is_never_followed(self):
        target = self.path / 'target.json'
        target.write_text('{"keep": true}', encoding='utf-8')
        self.output.symlink_to(target)
        self.run_cli(source=self.write(opening()), expect=2)
        self.assertEqual(target.read_text(encoding='utf-8'), '{"keep": true}')

    def test_illegal_opening_action_fails_the_whole_command(self):
        data = opening()
        data['actions'].append(action('A', 'check'))
        completed = self.run_cli(source=self.write(data), expect=2)
        self.assertEqual(completed.stdout, '')
        self.assertIn('error:', completed.stderr)
        self.assertFalse(self.output.exists())

    def test_bad_json_duplicate_keys_and_missing_source_exit_two(self):
        bad = self.path / 'bad.json'
        bad.write_text('{"start": ', encoding='utf-8')
        self.run_cli(source=bad, expect=2)
        duplicate = self.path / 'duplicate.json'
        duplicate.write_text(
            '{"start": {"button_seat": 0}, "start": {"button_seat": 0}}',
            encoding='utf-8')
        self.run_cli(source=duplicate, expect=2)
        self.run_cli(source=self.path / 'missing.json', expect=2)
        self.assertFalse(self.output.exists())

    def test_structural_input_errors_exit_two(self):
        cases = []
        data = opening()
        data['actions'][0]['player_id'] = 'Z'
        cases.append(data)
        data = opening()
        data['actions'][0] = {'player_id': 'A', 'kind': 'bet'}
        cases.append(data)
        data = opening()
        data['actions'][0] = {'player_id': 'A', 'kind': 'raise_to'}
        cases.append(data)
        data = opening()
        del data['actions']
        cases.append(data)
        data = opening()
        data['start']['players'][0]['stack'] = 1
        cases.append(data)
        data = opening()
        data['start']['ante'] = 0
        cases.append(data)
        data = opening()
        data['start']['players'][1]['seat'] = 1
        cases.append(data)
        checkpoint = checkpoint_of(opening())
        cases.append(branches(checkpoint, []))
        cases.append(branches(checkpoint, [branch('same', []),
                                           branch('same', [])]))
        cases.append(branches(checkpoint, [branch('bad id!', [])]))
        cases.append(branches(checkpoint, [branch('ok', 'nope')]))
        cases.append(branches(checkpoint, [branch('ok', [
            {'player_id': 'Z', 'kind': 'fold'}])]))
        for index, payload in enumerate(cases):
            with self.subTest(index=index):
                self.run_cli(source=self.write(payload, f'case{index}.json'),
                             output=self.path / f'out{index}.json', expect=2)
                self.assertFalse((self.path / f'out{index}.json').exists())


if __name__ == '__main__':
    unittest.main()
