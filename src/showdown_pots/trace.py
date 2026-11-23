"""Batch traceability of marked (to-be-written-off) chips across v1 hands.

A roster of 2..6 players carries piles of ordinary chips with a quantity of
marked chips inside each pile.  Only the v1 hand records (gross commitments,
matched pots, uncalled returns, integer awards) are known; the physical
marked chips are indistinguishable inside the mixed pools, so every chip
routing that respects the v1 quantities is a possible full flow.

The marked chips are conserved globally, therefore the set of feasible final
holding vectors is never a plain per-player box: the most each player could
hold separately cannot be added into the most a group could hold together.
The feasible set is the base polytope of a submodular capacity function

    U(S) = maximum marked chips that can end the processed history held by
           the players in subset S  (S is a bit mask over the roster).

It is the multi-sink max-flow function of the time-expanded chip-movement
network, hence monotone and submodular, with U(empty) = 0 and
U(roster) = M (the conserved total of marked chips).  Every hand extends the
network by one shallow layer (self stay arcs plus independent matched-pool
nodes), and the polymatroid max-flow/min-cut theorem gives the new capacity
of a target subset T as

    U'(T) = min over source-side boundary subsets X of
              U(P\\X) + stay(X & T)
              + sum over pools k of min(in_k(X), out_k(T))

where stay_p is the amount that never enters a matched pool (un-staked chips
plus the player's own uncalled return), in_k(X) is pool k's contribution
capacity coming from X and out_k(T) is pool k's award capacity landing in T.
All arithmetic is on aggregate quantities: running time is independent of
the chip magnitudes and never enumerates individual chips or integer
splits.
"""
import json

from .checks import array, fields, ident, integer, unique
from .service import process

CHECKPOINT_VERSION = 1
CHECKPOINT_LIMIT = 2048
MIN_ROSTER = 2
MAX_ROSTER = 6
MAX_HANDS = 1000
INITIAL_LIMIT = 1_000_000_000
COUNTED_LIMIT = 6_000_000_000


class TraceError(ValueError):
    """A structurally invalid or unplayable trace source (CLI exit 2)."""


# ---------------------------------------------------------------------------
# Source parsing
#


def _roster_from_initial(initial):
    if not isinstance(initial, dict):
        raise TraceError('initial must be a JSON object')
    fields(initial, ('balances', 'marked'))
    balances = initial['balances']
    marked = initial['marked']
    if not isinstance(balances, dict) or not isinstance(marked, dict):
        raise TraceError('balances and marked must be JSON objects')
    ids = list(balances)
    if set(marked) != set(ids):
        raise TraceError('balances and marked must name the same players')
    array(ids, MIN_ROSTER, MAX_ROSTER)
    for player_id in ids:
        ident(player_id)
    unique(ids)
    roster = sorted(ids)
    totals = {}
    for player_id in roster:
        balance = balances[player_id]
        if type(balance) is not int or not 0 <= balance <= INITIAL_LIMIT:
            raise TraceError(
                f'balance for {player_id} must be an integer in '
                f'[0,{INITIAL_LIMIT}]')
        marked_value = marked[player_id]
        if type(marked_value) is not int or not 0 <= marked_value <= balance:
            raise TraceError(
                f'marked count for {player_id} must be an integer between 0 '
                'and the balance')
        totals[player_id] = balance
    return roster, totals


def _parse_groups(value, roster):
    groups = []
    array(value, 0, 64)
    seen = set()
    for item in value:
        if not isinstance(item, dict):
            raise TraceError('group must be a JSON object')
        fields(item, ('id', 'players'))
        group_id = ident(item['id'])
        if group_id in seen:
            raise TraceError(f'duplicate group id: {group_id}')
        seen.add(group_id)
        players = item['players']
        array(players, 1, len(roster))
        for player_id in players:
            if not isinstance(player_id, str) or player_id not in roster:
                raise TraceError(
                    f'group {group_id} references unknown player: '
                    f'{player_id!r}')
        unique(players)
        groups.append((group_id, tuple(sorted(players))))
    return groups


def _parse_counted(value, roster):
    if not isinstance(value, dict):
        raise TraceError('counted must be a JSON object')
    if set(value) != set(roster):
        raise TraceError('counted must cover exactly the full roster once')
    counted = {}
    for player_id in roster:
        amount = value[player_id]
        if type(amount) is not int or not 0 <= amount <= COUNTED_LIMIT:
            raise TraceError(
                f'counted value for {player_id} must be an integer in '
                f'[0,{COUNTED_LIMIT}]')
        counted[player_id] = amount
    return counted


# ---------------------------------------------------------------------------
# Checkpoint (self-contained shift hand-off, no history beyond constraints)
#


def encode_checkpoint(checkpoint):
    """Canonical transfer encoding: sorted keys, compact, raw UTF-8."""
    return json.dumps(checkpoint, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':')).encode('utf-8')


def build_checkpoint(roster, totals, marked_total, capacities):
    checkpoint = {
        'v': CHECKPOINT_VERSION,
        'ids': list(roster),
        't': {player_id: totals[player_id] for player_id in roster},
        'm': marked_total,
        'u': list(capacities),
    }
    encoded = encode_checkpoint(checkpoint)
    if len(encoded) > CHECKPOINT_LIMIT:
        raise TraceError(
            f'checkpoint exceeds the {CHECKPOINT_LIMIT}-byte transfer limit')
    return checkpoint


def parse_checkpoint(value):
    if not isinstance(value, dict):
        raise TraceError('checkpoint must be a JSON object')
    fields(value, ('v', 'ids', 't', 'm', 'u'))
    if type(value['v']) is not int or value['v'] != CHECKPOINT_VERSION:
        raise TraceError(
            f"unsupported checkpoint version: {value['v']!r}")
    ids = value['ids']
    array(ids, MIN_ROSTER, MAX_ROSTER)
    for player_id in ids:
        ident(player_id)
    unique(ids)
    roster = sorted(ids)
    if list(ids) != roster:
        raise TraceError('checkpoint ids must be sorted')
    table = value['t']
    if not isinstance(table, dict) or set(table) != set(roster):
        raise TraceError('checkpoint totals must cover exactly its roster')
    totals = {}
    for player_id in roster:
        amount = table[player_id]
        if type(amount) is not int or not 0 <= amount <= COUNTED_LIMIT:
            raise TraceError(
                f'checkpoint total for {player_id} must be a non-negative '
                f'integer up to {COUNTED_LIMIT}')
        totals[player_id] = amount
    marked_total = value['m']
    if type(marked_total) is not int or not 0 <= marked_total <= sum(
            totals.values()):
        raise TraceError(
            'checkpoint marked total must be an integer between 0 and the '
            'roster totals')
    capacities = value['u']
    if not isinstance(capacities, list) or len(capacities) != 1 << len(
            roster):
        raise TraceError('checkpoint capacity table has the wrong length')
    size = 1 << len(roster)
    full = size - 1
    pile_sums = [0] * size
    for mask in range(size):
        amount = capacities[mask]
        if type(amount) is not int or amount < 0:
            raise TraceError('checkpoint capacities must be non-negative '
                             'integers')
        pile_sums[mask] = sum(
            totals[roster[index]]
            for index in range(len(roster))
            if mask & (1 << index))
        if amount > pile_sums[mask]:
            raise TraceError(
                'checkpoint capacity exceeds the chips held by its subset')
    if capacities[0] != 0:
        raise TraceError('capacity of the empty subset must be 0')
    if capacities[full] != marked_total:
        raise TraceError('capacity of the full roster must equal the marked '
                         'total')
    for mask in range(size):
        for index in range(len(roster)):
            other = mask | (1 << index)
            if other != mask and capacities[mask] > capacities[other]:
                raise TraceError('checkpoint capacity table is not monotone')
    for left in range(size):
        for right in range(left + 1, size):
            if capacities[left] + capacities[right] < \
                    capacities[left | right] + capacities[left & right]:
                raise TraceError(
                    'checkpoint capacity table is not submodular')
    return roster, totals, marked_total, list(capacities)


# ---------------------------------------------------------------------------
# Per-hand flow layer and capacity update
#


def _hand_layer(report, index, roster, totals):
    """Build the one-hop marked-chip network of one settled v1 hand."""
    n = len(roster)
    size = 1 << n
    full = size - 1
    commits = {player_id: 0 for player_id in roster}
    awards = {player_id: 0 for player_id in roster}
    returns = {player_id: 0 for player_id in roster}
    for entry in report['players']:
        player_id = entry['player_id']
        if player_id not in index:
            raise TraceError(
                f'hand participant {player_id} is not on the roster')
        commits[player_id] = entry['committed']
        awards[player_id] = entry['pot_awards']
        returns[player_id] = entry['uncalled_return']
        if commits[player_id] > totals[player_id]:
            raise TraceError(
                f'gross commitment of {player_id} ({commits[player_id]}) '
                f'exceeds the {totals[player_id]} chips held at the start '
                'of the hand')
    stay_per_player = {
        player_id:
        totals[player_id] - commits[player_id] + returns[player_id]
        for player_id in roster
    }
    stay = [0] * size
    for mask in range(1, size):
        bit = mask & -mask
        player_id = roster[bit.bit_length() - 1]
        stay[mask] = stay[mask ^ bit] + stay_per_player[player_id]
    pools = []
    for pot in report['pots']:
        contribution = pot['to_inclusive'] - pot['from_exclusive']
        contributor_mask = 0
        for player_id in pot['contributor_ids']:
            contributor_mask |= 1 << index[player_id]
        outgoing = [0] * size
        for player_id, amount in pot['awards'].items():
            bit = 1 << index[player_id]
            for mask in range(size):
                if mask & bit:
                    outgoing[mask] += amount
        pools.append((contributor_mask, contribution, outgoing))
    new_totals = dict(totals)
    for player_id in roster:
        new_totals[player_id] = (
            totals[player_id] - commits[player_id]
            + awards[player_id] + returns[player_id])
    return stay, pools, new_totals


def advance(capacities, stay, pools, roster_size):
    """One polymatroid max-flow/min-cut capacity update."""
    size = 1 << roster_size
    full = size - 1
    updated = [0] * size
    for target in range(1, size):
        outgoing = [pool[2][target] for pool in pools]
        best = None
        for boundary in range(size):
            value = capacities[full ^ boundary] + stay[target & boundary]
            for pool_index, (contributor_mask, contribution, _) in enumerate(
                    pools):
                inflow = contribution * (
                    contributor_mask & boundary).bit_count()
                value += min(inflow, outgoing[pool_index])
            if best is None or value < best:
                best = value
        updated[target] = best
    if updated[full] != capacities[full]:
        raise TraceError('internal marked-chip conservation failed')
    return updated


# ---------------------------------------------------------------------------
# Queries over the terminal feasible base polytope
#


def _mask_of(roster_index, players):
    mask = 0
    for player_id in players:
        mask |= 1 << roster_index[player_id]
    return mask


def group_report(roster, totals, capacities, marked_total, groups):
    roster_index = {player_id: i for i, player_id in enumerate(roster)}
    size = 1 << len(roster)
    full = size - 1
    pile_sums = [0] * size
    for mask in range(1, size):
        bit = mask & -mask
        pile_sums[mask] = (
            pile_sums[mask ^ bit] + totals[roster[bit.bit_length() - 1]])
    result = []
    for group_id, players in sorted(groups):
        mask = _mask_of(roster_index, players)
        marked_max = capacities[mask]
        marked_min = marked_total - capacities[full ^ mask]
        result.append({
            'id': group_id,
            'marked_min': marked_min,
            'marked_max': marked_max,
            'unmarked_min': pile_sums[mask] - marked_max,
        })
    return result


def counted_possible(roster, totals, capacities, marked_total, counted):
    if counted is None:
        return None
    roster_index = {player_id: i for i, player_id in enumerate(roster)}
    size = 1 << len(roster)
    full = size - 1
    if sum(counted.values()) != marked_total:
        return False
    for player_id in roster:
        if counted[player_id] > totals[player_id]:
            return False
    subset_sums = [0] * size
    for mask in range(1, size):
        bit = mask & -mask
        subset_sums[mask] = (
            subset_sums[mask ^ bit]
            + counted[roster[bit.bit_length() - 1]])
        if subset_sums[mask] > capacities[mask]:
            return False
    if subset_sums[full] != marked_total:
        return False
    return True


# ---------------------------------------------------------------------------
# Entry point
#


def trace(data):
    if not isinstance(data, dict):
        raise TraceError('source must be a JSON object')
    keys = set(data)
    if not {'hands', 'groups'} <= keys:
        raise TraceError('hands and groups are required')
    if keys - {'hands', 'groups', 'counted', 'initial', 'checkpoint'}:
        raise TraceError('unexpected top-level fields')
    if ('initial' in data) == ('checkpoint' in data):
        raise TraceError(
            'exactly one of initial or checkpoint must be provided')

    if 'initial' in data:
        roster, totals = _roster_from_initial(data['initial'])
        marked_total = sum(data['initial']['marked'][player_id]
                           for player_id in roster)
        size = 1 << len(roster)
        capacities = [0] * size
        roster_index = {player_id: i for i, player_id in enumerate(roster)}
        for mask in range(1, size):
            bit = mask & -mask
            capacities[mask] = (
                capacities[mask ^ bit]
                + data['initial']['marked'][roster[bit.bit_length() - 1]])
    else:
        roster, totals, marked_total, capacities = parse_checkpoint(
            data['checkpoint'])
        roster_index = {player_id: i for i, player_id in enumerate(roster)}

    groups = _parse_groups(data['groups'], roster)
    counted = (_parse_counted(data['counted'], roster)
               if 'counted' in data else None)

    hands = data['hands']
    array(hands, 0, MAX_HANDS)
    for hand in hands:
        if not isinstance(hand, dict):
            raise TraceError('each hand must be a v1 input object')
        report = process(hand)
        stay, pools, totals = _hand_layer(
            report, roster_index, roster, totals)
        capacities = advance(capacities, stay, pools, len(roster))

    checkpoint = build_checkpoint(
        roster, totals, marked_total, capacities)
    return {
        'balances': {player_id: totals[player_id] for player_id in roster},
        'groups': group_report(
            roster, totals, capacities, marked_total, groups),
        'counted_possible': counted_possible(
            roster, totals, capacities, marked_total, counted),
        'checkpoint': checkpoint,
    }
