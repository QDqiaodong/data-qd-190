"""Replay a recorded teaching-table hand, one betting street at a time.

The command accepts either a complete opening replay or a checkpoint produced
by an earlier run.  It applies recorded actions under the teaching-table rules
(three streets, integer chips, antes already paid at the opening), reports the
options of the player who must act next, and settles only a finished hand via
the unchanged v1 single-hand rules in :mod:`showdown_pots.service`.

A checkpoint is self-contained: it carries derived state only, never the
action history, so a fresh process can resume from it.  Its canonical JSON
encoding (sorted keys, compact separators, unescaped non-ASCII) must stay
within the 4096-byte terminal budget.
"""
import copy
import json

from .cards import card
from .checks import array, fields, ident, integer, unique
from .service import process

CHECKPOINT_VERSION = 1
CHECKPOINT_LIMIT = 4096
STREETS = ('flop', 'turn', 'river')
KINDS = ('check', 'call', 'fold', 'raise_to')


class ReplayError(ValueError):
    """A malformed source (CLI exit status 2)."""


class IllegalAction(ValueError):
    """A structurally valid action that the table rules do not allow."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


# ---------------------------------------------------------------------------
# Parsing


def _action(item, known_ids):
    if not isinstance(item, dict):
        raise ReplayError('action must be a JSON object')
    if 'player_id' not in item or 'kind' not in item:
        raise ReplayError('action requires player_id and kind')
    kind = item['kind']
    if not isinstance(kind, str) or kind not in KINDS:
        raise ReplayError('action kind must be check, call, fold or raise_to')
    if kind == 'raise_to':
        fields(item, ('player_id', 'kind', 'to'))
        target = integer(item['to'], 1, 1_000_000_000)
    else:
        fields(item, ('player_id', 'kind'))
        target = None
    player_id = ident(item['player_id'])
    if player_id not in known_ids:
        raise ReplayError(f'unknown player id: {player_id}')
    return {'player_id': player_id, 'kind': kind, 'to': target}


def _actions(value, known_ids):
    return [_action(item, known_ids)
            for item in array(value, 0, 256)]


def _players(value, ante):
    players = []
    for item in array(value, 2, 9):
        if not isinstance(item, dict):
            raise ReplayError('player must be a JSON object')
        fields(item, ('player_id', 'seat', 'stack', 'hole'))
        stack = integer(item['stack'], ante + 1, 1_000_000_000)
        player_id = ident(item['player_id'])
        seat = integer(item['seat'], 0, 8)
        hole = [card(value) for value in array(item['hole'], 2, 2)]
        players.append({
            'id': player_id,
            'seat': seat,
            'stack': stack,
            'committed': ante,
            'street_committed': 0,
            'folded': False,
            'hole': hole,
            'acted': False,
            'base': None,
        })
    unique([p['id'] for p in players])
    unique([p['seat'] for p in players])
    players.sort(key=lambda p: p['seat'])
    return players


def _build_start(start):
    fields(start, ('button_seat', 'board', 'ante', 'min_bet', 'players'))
    button = integer(start['button_seat'], 0, 8)
    board = [card(value) for value in array(start['board'], 5, 5)]
    ante = integer(start['ante'], 1, 1_000_000_000)
    min_bet = integer(start['min_bet'], 1, 1_000_000_000)
    players = _players(start['players'], ante)
    unique(board + [value for p in players for value in p['hole']])
    for player in players:
        player['stack'] -= ante
    hand = {
        'button_seat': button,
        'board': board,
        'min_bet': min_bet,
        'street': 0,
        'done': False,
        'price': 0,
        'min_raise': min_bet,
        'next': None,
        'players': players,
    }
    _arrive(hand, button)
    return hand


def _build_checkpoint(value):
    if not isinstance(value, dict):
        raise ReplayError('checkpoint must be a JSON object')
    fields(value, ('version', 'button_seat', 'board', 'min_bet', 'street',
                   'done', 'price', 'min_raise', 'next', 'players'))
    version = integer(value['version'], 0, 1_000_000_000)
    if version != CHECKPOINT_VERSION:
        raise ReplayError(f'unsupported checkpoint version: {version}')
    button = integer(value['button_seat'], 0, 8)
    board = [card(entry) for entry in array(value['board'], 5, 5)]
    min_bet = integer(value['min_bet'], 1, 1_000_000_000)
    street = integer(value['street'], 0, 2)
    if type(value['done']) is not bool:
        raise ReplayError('checkpoint done must be boolean')
    done = value['done']
    price = integer(value['price'], 0, 1_000_000_000)
    min_raise = integer(value['min_raise'], 1, 1_000_000_000)
    if value['next'] is None:
        next_id = None
    else:
        next_id = ident(value['next'])
    players = []
    for item in array(value['players'], 2, 9):
        if not isinstance(item, dict):
            raise ReplayError('checkpoint player must be a JSON object')
        fields(item, ('id', 'seat', 'stack', 'committed', 'street_committed',
                      'folded', 'hole', 'acted', 'base'))
        player_id = ident(item['id'])
        seat = integer(item['seat'], 0, 8)
        stack = integer(item['stack'], 0, 1_000_000_000)
        committed = integer(item['committed'], 1, 1_000_000_000)
        street_committed = integer(item['street_committed'], 0, price)
        if street_committed > committed:
            raise ReplayError('checkpoint street commitment exceeds total')
        if type(item['folded']) is not bool:
            raise ReplayError('checkpoint folded must be boolean')
        folded = item['folded']
        hole = [card(entry) for entry in array(item['hole'], 2, 2)]
        if type(item['acted']) is not bool:
            raise ReplayError('checkpoint acted must be boolean')
        acted = item['acted']
        if acted:
            base = integer(item['base'], 0, price)
        else:
            if item['base'] is not None:
                raise ReplayError('inactive player base must be null')
            base = None
        players.append({
            'id': player_id,
            'seat': seat,
            'stack': stack,
            'committed': committed,
            'street_committed': street_committed,
            'folded': folded,
            'hole': hole,
            'acted': acted,
            'base': base,
        })
    unique([p['id'] for p in players])
    unique([p['seat'] for p in players])
    unique(board + [value for p in players for value in p['hole']])
    players.sort(key=lambda p: p['seat'])
    live = [p for p in players if not p['folded']]
    if not live:
        raise ReplayError('checkpoint requires at least one live player')
    by_id = {p['id']: p for p in players}
    hand = {
        'button_seat': button,
        'board': board,
        'min_bet': min_bet,
        'street': street,
        'done': done,
        'price': price,
        'min_raise': min_raise,
        'next': next_id,
        'players': players,
    }
    if price != max(p['street_committed'] for p in players):
        raise ReplayError('checkpoint price must equal the highest street '
                          'commitment')
    pending_ids = {p['id'] for p in _pending(hand)}
    if done:
        if next_id is not None:
            raise ReplayError('finished checkpoint must have null next')
        finished = (len(live) == 1) or (street == 2 and not pending_ids)
        if not finished:
            raise ReplayError('checkpoint claims a finished hand but play '
                              'is still pending')
    else:
        if len(live) < 2:
            raise ReplayError('unfinished checkpoint requires two live players')
        if next_id is None:
            raise ReplayError('unfinished checkpoint requires a next player')
        if next_id not in by_id:
            raise ReplayError(f'checkpoint next references unknown player: {next_id}')
        actor = by_id[next_id]
        if actor['folded'] or actor['stack'] <= 0:
            raise ReplayError('checkpoint next player must be live with chips')
        if next_id not in pending_ids:
            raise ReplayError('checkpoint next player is not due to act')
    return hand


# ---------------------------------------------------------------------------
# Table state and rules


def _live(hand):
    return [p for p in hand['players'] if not p['folded']]


def _funded(hand):
    return [p for p in hand['players'] if not p['folded'] and p['stack'] > 0]


def _to_call(hand, player):
    return hand['price'] - player['street_committed']


def _pending(hand):
    funded = _funded(hand)
    if len(funded) <= 1:
        if funded and _to_call(hand, funded[0]) > 0:
            return funded
        return []
    return [p for p in funded
            if not p['acted'] or _to_call(hand, p) > 0]


def _first_after(hand, after_seat):
    pending = _pending(hand)
    if not pending:
        return None
    def distance(player):
        delta = (player['seat'] - after_seat) % 9
        return 9 if delta == 0 else delta
    return min(pending, key=distance)['id']


def _reset_street(hand):
    hand['price'] = 0
    hand['min_raise'] = hand['min_bet']
    for player in hand['players']:
        player['street_committed'] = 0
        player['acted'] = False
        player['base'] = None


def _arrive(hand, after_seat):
    """Close finished streets until an actor is due or the hand finishes."""
    while not hand['done']:
        if len(_live(hand)) <= 1:
            hand['done'] = True
            hand['next'] = None
            return
        if _pending(hand):
            hand['next'] = _first_after(hand, after_seat)
            return
        if hand['street'] == 2:
            hand['done'] = True
            hand['next'] = None
            return
        hand['street'] += 1
        _reset_street(hand)
        after_seat = hand['button_seat']


def _by_id(hand, player_id):
    for player in hand['players']:
        if player['id'] == player_id:
            return player
    raise ReplayError(f'unknown player id: {player_id}')


def _can_raise(hand, player):
    if not any(other is not player and not other['folded'] and other['stack'] > 0
               for other in hand['players']):
        return False
    if player['acted'] and hand['price'] - player['base'] < hand['min_raise']:
        return False
    return True


def _apply(hand, action):
    if hand['done']:
        raise IllegalAction('the hand is already finished')
    actor = _by_id(hand, hand['next'])
    if action['player_id'] != actor['id']:
        raise IllegalAction(
            f"it is {actor['id']}'s turn, not {action['player_id']}'s")
    kind = action['kind']
    owed = _to_call(hand, actor)

    if kind == 'fold':
        actor['folded'] = True
    elif kind == 'check':
        if owed != 0:
            raise IllegalAction(f'cannot check while owing {owed}')
    elif kind == 'call':
        if owed <= 0:
            raise IllegalAction('cannot call when nothing is owed')
        paid = min(owed, actor['stack'])
        actor['stack'] -= paid
        actor['committed'] += paid
        actor['street_committed'] += paid
    else:
        target = action['to']
        if target <= hand['price']:
            raise IllegalAction(
                f"raise target {target} must exceed the current price "
                f"{hand['price']}")
        if not _can_raise(hand, actor):
            if not any(other is not actor and not other['folded']
                       and other['stack'] > 0 for other in hand['players']):
                raise IllegalAction('no other live player has chips, so a '
                                    'raise is not allowed')
            raise IllegalAction(
                f"{actor['id']} has not faced a full raise of "
                f"{hand['min_raise']} since the last action")
        paid = target - actor['street_committed']
        if paid > actor['stack']:
            raise IllegalAction(
                f'raise target {target} exceeds {actor["id"]}\'s remaining '
                f'stack')
        increment = target - hand['price']
        if increment < hand['min_raise'] and paid != actor['stack']:
            raise IllegalAction(
                f'raise increment {increment} is below the minimum full '
                f'raise {hand["min_raise"]} and is not an all-in')
        actor['stack'] -= paid
        actor['committed'] += paid
        actor['street_committed'] = target
        if increment >= hand['min_raise']:
            hand['min_raise'] = increment
        hand['price'] = target

    actor['acted'] = True
    actor['base'] = hand['price']
    hand['next'] = None
    _arrive(hand, actor['seat'])


def _next_view(hand):
    if hand['done']:
        return None
    actor = _by_id(hand, hand['next'])
    owed = _to_call(hand, actor)
    view = {
        'player_id': actor['id'],
        'street': STREETS[hand['street']],
        'to_call': owed,
        'call_chips': min(owed, actor['stack']) if owed > 0 else 0,
        'can_check': owed == 0,
        'raise_to_min': None,
        'raise_to_max': None,
        'short_all_in_to': None,
    }
    if _can_raise(hand, actor):
        maximum = actor['street_committed'] + actor['stack']
        minimum = hand['price'] + hand['min_raise']
        if maximum >= minimum:
            view['raise_to_min'] = minimum
            view['raise_to_max'] = maximum
        elif maximum > hand['price']:
            view['short_all_in_to'] = maximum
    return view


def _settlement(hand):
    if not hand['done']:
        return None
    source = {
        'board': list(hand['board']),
        'button_seat': hand['button_seat'],
        'players': [
            {
                'id': player['id'],
                'seat': player['seat'],
                'committed': player['committed'],
                'folded': player['folded'],
                'hole': list(player['hole']),
            }
            for player in hand['players']
        ],
    }
    return process(source)


def _checkpoint(hand):
    value = {
        'version': CHECKPOINT_VERSION,
        'button_seat': hand['button_seat'],
        'board': list(hand['board']),
        'min_bet': hand['min_bet'],
        'street': hand['street'],
        'done': hand['done'],
        'price': hand['price'],
        'min_raise': hand['min_raise'],
        'next': hand['next'],
        'players': [
            {
                'id': player['id'],
                'seat': player['seat'],
                'stack': player['stack'],
                'committed': player['committed'],
                'street_committed': player['street_committed'],
                'folded': player['folded'],
                'hole': list(player['hole']),
                'acted': player['acted'],
                'base': player['base'],
            }
            for player in sorted(hand['players'], key=lambda p: p['seat'])
        ],
    }
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    if len(encoded.encode('utf-8')) > CHECKPOINT_LIMIT:
        raise ReplayError(
            f'checkpoint exceeds the {CHECKPOINT_LIMIT}-byte limit')
    return value


# ---------------------------------------------------------------------------
# Modes


def _replay_start(data):
    hand = _build_start(data['start'])
    known = {player['id'] for player in hand['players']}
    actions = _actions(data['actions'], known)
    for position, action in enumerate(actions, 1):
        try:
            _apply(hand, action)
        except IllegalAction as error:
            raise ReplayError(
                f'illegal action {position}: {error.reason}') from None
    return {
        'checkpoint': _checkpoint(hand),
        'next': _next_view(hand),
        'settlement': _settlement(hand),
    }


def _replay_branches(data):
    hand = _build_checkpoint(data['checkpoint'])
    known = {player['id'] for player in hand['players']}
    raw_branches = array(data['branches'], 1, 64)
    branches = []
    for item in raw_branches:
        if not isinstance(item, dict):
            raise ReplayError('branch must be a JSON object')
        fields(item, ('id', 'actions'))
        branch_id = ident(item['id'])
        branches.append({
            'id': branch_id,
            'actions': _actions(item['actions'], known),
        })
    unique([branch['id'] for branch in branches])
    branches.sort(key=lambda branch: branch['id'])

    results = []
    for branch in branches:
        working = copy.deepcopy(hand)
        try:
            for position, action in enumerate(branch['actions'], 1):
                _apply(working, action)
        except IllegalAction as error:
            results.append({
                'id': branch['id'],
                'status': 'illegal',
                'action_index': position,
                'reason': error.reason,
            })
            continue
        results.append({
            'id': branch['id'],
            'status': 'ok',
            'checkpoint': _checkpoint(working),
            'next': _next_view(working),
            'settlement': _settlement(working),
        })
    return {'branches': results}


def replay(data):
    """Validate and replay one source object."""
    if not isinstance(data, dict):
        raise ReplayError('source must be a JSON object')
    keys = set(data)
    if keys == {'start', 'actions'}:
        return _replay_start(data)
    if keys == {'checkpoint', 'branches'}:
        return _replay_branches(data)
    raise ReplayError(
        'source must contain exactly start+actions or checkpoint+branches')
