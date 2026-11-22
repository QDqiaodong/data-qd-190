"""Audit a partially occluded single hand.

The input follows the v1 single-hand shape, but individual board/hole positions
may be candidate slots (``{"slot_id", "candidates"}``) instead of fixed cards.
A completion fixes every slot to one of its candidates while keeping the whole
hand consistent with one physical deck (no card appears twice).  Every
completion is settled by the unchanged v1 rules in :mod:`showdown_pots.service`.

A pot is certified only when its per-player award map is identical in every
feasible completion.  Two exact techniques keep the search bounded:

* feasibility of the physical-deck constraint (all slots, including the folded
  hole slots that never reach a report) is answered by bipartite matching;
* when the remaining relevant slots are mutually independent (fixed board and
  disjoint candidate pools), possible winners are derived from each live
  player's attainable rank set instead of enumerating the joint product.

Coupled sub-problems fall back to memoised enumeration with cached seven-card
evaluations and early termination once every pot is known ambiguous.
"""
from functools import lru_cache

from .cards import best_seven, card
from .checks import array, fields, ident, integer, unique
from .service import process

MAX_SLOTS = 12


class AuditError(ValueError):
    """A malformed audit request (CLI exit status 2)."""


def _validate_dead_layer(players):
    """Reject matched contribution layers without an eligible live player.

    This depends only on contributions and folds, so it is an input error
    rather than a result of the unknown card evidence.  Mirrors pots.settle.
    """
    for level in sorted({p['committed'] for p in players if p['committed'] > 0}):
        contributors = [p for p in players if p['committed'] >= level]
        if len(contributors) != 1 and not any(not p['folded'] for p in contributors):
            raise AuditError('matched contribution layer has no eligible live player')


def _location(entry):
    """Parse one board/hole position into either a fixed card or a slot."""
    if isinstance(entry, str):
        return ('card', card(entry))
    if not isinstance(entry, dict):
        raise AuditError('card position must be a card string or a slot object')
    fields(entry, ('slot_id', 'candidates'))
    slot_id = ident(entry['slot_id'])
    choices = [card(value) for value in array(entry['candidates'], 1, 4)]
    unique(choices)
    return ('slot', slot_id, choices)


def _parse_request(data):
    if not isinstance(data, dict):
        raise AuditError('request must be a JSON object')
    fields(data, ('hand', 'inspectable', 'budget'))
    integer(data['budget'], 0, 2)
    inspectable = data['inspectable']
    if not isinstance(inspectable, list):
        raise AuditError('inspectable must be an array of slot ids')
    inspectable = [ident(value) for value in inspectable]
    unique(inspectable)

    hand = data['hand']
    if not isinstance(hand, dict):
        raise AuditError('hand must be a JSON object')
    fields(hand, ('board', 'players', 'button_seat'))
    button_seat = integer(hand['button_seat'], 0, 8)

    board = []
    slots = []
    relevant = set()
    fixed_cards = []
    for entry in array(hand['board'], 5, 5):
        location = _location(entry)
        board.append(location)
        if location[0] == 'card':
            fixed_cards.append(location[1])
        else:
            slots.append(location)
            relevant.add(location[1])  # board positions feed every live hand

    players = []
    for player in array(hand['players'], 2, 9):
        if not isinstance(player, dict):
            raise AuditError('each player must be an object')
        fields(player, ('id', 'seat', 'committed', 'folded', 'hole'))
        player_id = ident(player['id'])
        seat = integer(player['seat'], 0, 8)
        committed = integer(player['committed'], 0, 1_000_000_000)
        if type(player['folded']) is not bool:
            raise AuditError('folded must be boolean')
        hole_raw = array(player['hole'], 0 if player['folded'] else 2, 2)
        if len(hole_raw) not in (0, 2):
            raise AuditError('folded hole must be absent or two positions')
        hole = []
        for entry in hole_raw:
            location = _location(entry)
            hole.append(location)
            if location[0] == 'card':
                fixed_cards.append(location[1])
            else:
                slots.append(location)
                if not player['folded']:
                    relevant.add(location[1])
        players.append({
            'id': player_id, 'seat': seat, 'committed': committed,
            'folded': player['folded'], 'hole': hole,
        })

    unique([p['id'] for p in players])
    unique([p['seat'] for p in players])
    if not any(not p['folded'] for p in players):
        raise AuditError('at least one live player required')
    if not sum(p['committed'] for p in players):
        raise AuditError('positive contribution required')
    _validate_dead_layer(players)

    if len(slots) > MAX_SLOTS:
        raise AuditError(f'at most {MAX_SLOTS} candidate slots per hand')
    slot_ids = [slot[1] for slot in slots]
    unique(slot_ids)
    unknown = [slot_id for slot_id in inspectable if slot_id not in set(slot_ids)]
    if unknown:
        raise AuditError(f'inspectable slot without evidence: {", ".join(unknown)}')

    return {
        'board': board,
        'players': players,
        'button_seat': button_seat,
        'slots': slots,
        'relevant': frozenset(relevant),
        'inspectable': inspectable,
        'budget': data['budget'],
        'fixed_cards': fixed_cards,
    }


def _has_matching(state_map, used_cards):
    """Bipartite matching: can every remaining slot take a still-free card?"""
    match = {}

    def augment(slot_id, seen):
        for candidate in state_map[slot_id]:
            if candidate in used_cards or candidate in seen:
                continue
            seen.add(candidate)
            if candidate not in match or augment(match[candidate], seen):
                match[candidate] = slot_id
                return True
        return False

    for slot_id in sorted(state_map):
        if not augment(slot_id, set()):
            return False
    return True


class _AuditModel:
    """All searches for one parsed request, with memoised sub-problems."""

    def __init__(self, parsed):
        self.board = parsed['board']
        self.players = parsed['players']
        self.button_seat = parsed['button_seat']
        self.inspectable = parsed['inspectable']
        self.budget = parsed['budget']
        self.fixed_cards = frozenset(parsed['fixed_cards'])
        self.relevant = parsed['relevant']
        self.slot_order = sorted(slot[1] for slot in parsed['slots'])
        self.initial_state = frozenset(
            (slot[1], tuple(sorted(slot[2]))) for slot in parsed['slots']
        )
        # Where each slot sits: ('board', index) or ('hole', player index, index).
        self.slot_positions = {}
        for index, location in enumerate(self.board):
            if location[0] == 'slot':
                self.slot_positions[location[1]] = ('board', index)
        for player_index, player in enumerate(self.players):
            for index, location in enumerate(player['hole']):
                if location[0] == 'slot':
                    self.slot_positions[location[1]] = (
                        'hole', player_index, index)
        self._report_cache = {}
        self._rank_cache = {}

    def _all_locations(self):
        yield from self.board
        for player in self.players:
            yield from player['hole']

    @staticmethod
    def _inconsistent():
        return {
            'status': 'inconsistent',
            'pots': [],
            'uncalled_returns': [],
            'witnesses': [],
            'plan': None,
        }

    # -- completion materialisation ---------------------------------------

    def _render(self, locations, assignment):
        return [
            location[1] if location[0] == 'card'
            else assignment[location[1]]
            for location in locations
        ]

    def _build_hand(self, assignment):
        return {
            'board': self._render(self.board, assignment),
            'button_seat': self.button_seat,
            'players': [
                {
                    'id': player['id'],
                    'seat': player['seat'],
                    'committed': player['committed'],
                    'folded': player['folded'],
                    'hole': self._render(player['hole'], assignment),
                }
                for player in self.players
            ],
        }

    def report(self, assignment):
        key = tuple(assignment.get(slot_id) for slot_id in self.slot_order)
        cached = self._report_cache.get(key)
        if cached is None:
            cached = process(self._build_hand(assignment))
            self._report_cache[key] = cached
        return cached

    def _rank(self, board_cards, player_index, hole_cards):
        key = (tuple(board_cards), player_index, tuple(hole_cards))
        cached = self._rank_cache.get(key)
        if cached is None:
            cached = tuple(best_seven(list(board_cards) + list(hole_cards))['rank'])
            self._rank_cache[key] = cached
        return cached

    # -- feasibility --------------------------------------------------------

    @staticmethod
    def _state_after(state, slot_id, card_value):
        return frozenset(
            (other, tuple(c for c in choices if c != card_value))
            for other, choices in state
            if other != slot_id
        )

    @lru_cache(maxsize=None)
    def solve_one(self, state):
        """Return one feasible full assignment for a sub-problem, or None."""
        state_map = dict(state)
        if not state_map:
            return ()
        if not _has_matching(state_map, self.fixed_cards):
            return None
        slot_id = min(
            state_map,
            key=lambda sid: (
                len([c for c in state_map[sid] if c not in self.fixed_cards]),
                sid,
            ),
        )
        for card_value in state_map[slot_id]:
            if card_value in self.fixed_cards:
                continue
            rest = self.solve_one(self._state_after(state, slot_id, card_value))
            if rest is not None:
                return (slot_id, card_value) + rest
        return None

    def _extend(self, state):
        """Concrete cards for every slot still open in ``state``."""
        solution = self.solve_one(state)
        if solution is None:
            return None
        return dict(zip(solution[0::2], solution[1::2]))

    # -- independent (symbolic) tier ---------------------------------------

    def _independent_tier(self, state, prefix):
        """Return per-slot choice info when slots do not interact.

        The independent tier applies when the board has no open slot and no
        candidate card is shared between two open slots or pinned by a fixed
        card: every live player's attainable ranks can then be explored
        without tracking the other players' choices.
        """
        state_map = dict(state)
        if any(self.slot_positions[slot_id][0] == 'board'
               for slot_id in state_map):
            return None
        seen_cards = set()
        for choices in state_map.values():
            for card_value in choices:
                if card_value in self.fixed_cards or card_value in seen_cards:
                    return None
                seen_cards.add(card_value)
        observed = dict(prefix)
        board_cards = self._render(self.board, observed)
        players = []
        for player_index, player in enumerate(self.players):
            if player['folded']:
                players.append(None)
                continue
            open_slots = []
            fixed_hole = []
            for location in player['hole']:
                if location[0] == 'card':
                    fixed_hole.append(location[1])
                elif location[1] in state_map:
                    open_slots.append(location[1])
                else:
                    fixed_hole.append(observed[location[1]])
            players.append((player_index, open_slots, fixed_hole))
        return board_cards, players, state_map

    def _player_options(self, tier, player_info):
        """All distinct (rank, slot assignment) options for one player."""
        board_cards, _, state_map = tier
        player_index, open_slots, fixed_hole = player_info
        options = []
        seen_ranks = set()

        def expand(index, assignment, hole_cards):
            if index == len(open_slots):
                rank = self._rank(board_cards, player_index, hole_cards)
                if rank not in seen_ranks:
                    seen_ranks.add(rank)
                    options.append((rank, dict(assignment)))
                return
            slot_id = open_slots[index]
            for card_value in state_map[slot_id]:
                assignment[slot_id] = card_value
                hole_cards.append(card_value)
                expand(index + 1, assignment, hole_cards)
                hole_cards.pop()
                del assignment[slot_id]

        expand(0, {}, list(fixed_hole))
        return options

    # -- coupled enumeration tier ------------------------------------------

    def _relevant_walk(self, state, used, partial):
        """Yield (relevant assignment, leftover state) completion classes."""
        state_map = dict(state)
        if not _has_matching(state_map, self.fixed_cards | used):
            return
        open_relevant = [slot_id for slot_id in state_map
                         if slot_id in self.relevant]
        if not open_relevant:
            yield dict(partial), state
            return
        slot_id = min(
            open_relevant,
            key=lambda sid: (
                len([c for c in state_map[sid]
                     if c not in self.fixed_cards and c not in used]),
                sid,
            ),
        )
        for card_value in state_map[slot_id]:
            if card_value in self.fixed_cards or card_value in used:
                continue
            partial[slot_id] = card_value
            yield from self._relevant_walk(
                self._state_after(state, slot_id, card_value),
                used | {card_value}, partial)
            del partial[slot_id]

    @lru_cache(maxsize=None)
    def count_completions(self, state, used):
        """Number of report-relevant completion classes of a sub-problem."""
        state_map = dict(state)
        if not _has_matching(state_map, self.fixed_cards | used):
            return 0
        open_relevant = [slot_id for slot_id in state_map
                         if slot_id in self.relevant]
        if not open_relevant:
            return 1
        slot_id = min(
            open_relevant,
            key=lambda sid: (
                len([c for c in state_map[sid]
                     if c not in self.fixed_cards and c not in used]),
                sid,
            ),
        )
        return sum(
            self.count_completions(
                self._state_after(state, slot_id, card_value),
                used | {card_value})
            for card_value in state_map[slot_id]
            if card_value not in self.fixed_cards and card_value not in used
        )

    @lru_cache(maxsize=None)
    def aggregate(self, state, prefix):
        """Settle every completion class of a sub-problem.

        ``prefix`` is the sorted tuple of (slot, observed card) observations
        already made; it uniquely identifies this node alongside ``state``.
        Returns None when the observation path has no feasible completion.
        """
        observed_cards = [card_value for _, card_value in prefix]
        if len(set(observed_cards)) != len(observed_cards) \
                or self.fixed_cards.intersection(observed_cards):
            return None
        if self.solve_one(state) is None:
            return None

        tier = self._independent_tier(state, prefix)
        if tier is not None:
            return self._aggregate_independent(state, prefix, tier)
        return self._aggregate_enumerated(state, prefix)

    def _seed_summary(self, full):
        report = self.report(full)
        return {
            'first': full,
            'first_report': report,
            'template_pots': report['pots'],
            'pot_maps': [dict(pot['awards']) for pot in report['pots']],
            'differing': set(),
            'second': None,
            'second_report': None,
        }

    def _note(self, summary, full, report):
        for index, pot in enumerate(report['pots']):
            if dict(pot['awards']) != summary['pot_maps'][index]:
                summary['differing'].add(index)
                if summary['second'] is None:
                    summary['second'] = full
                    summary['second_report'] = report

    def _aggregate_enumerated(self, state, prefix):
        prefix_map = dict(prefix)
        used = frozenset(card_value for _, card_value in prefix)
        summary = None
        for relevant_assignment, leftover in self._relevant_walk(state, used, {}):
            extension = self._extend(leftover)
            if extension is None:
                continue
            full = {**prefix_map, **relevant_assignment, **extension}
            if summary is None:
                summary = self._seed_summary(full)
                continue
            self._note(summary, full, self.report(full))
            if len(summary['differing']) == len(summary['pot_maps']):
                break  # every pot already known ambiguous
        return summary

    def _aggregate_independent(self, state, prefix, tier):
        """Symbolic aggregation over per-player attainable rank sets."""
        _, players_info, _ = tier
        live_infos = [info for info in players_info if info is not None]
        options_by_player = [
            self._player_options(tier, info) for info in live_infos
        ]
        # One representative completion seeds the v1-shaped summary; folded
        # and observed slots are extended through the matching oracle.
        representative = dict(prefix)
        for options in options_by_player:
            representative.update(options[0][1])
        extension = self._extend(frozenset(
            (slot_id, choices) for slot_id, choices in state
            if slot_id not in representative))
        if extension is None:
            return None
        representative.update(extension)
        summary = self._seed_summary(representative)

        pot_layers = [
            [player['id'] for player in self.players
             if player['committed'] >= pot['to_inclusive']
             and not player['folded']]
            for pot in summary['template_pots']
        ]
        player_ids = [self.players[info[0]]['id'] for info in live_infos]
        rank_sets = [
            {option[0] for option in options}
            for options in options_by_player
        ]
        for pot_index, eligible in enumerate(pot_layers):
            eligible_sets = [
                (player_id, ranks)
                for player_id, ranks in zip(player_ids, rank_sets)
                if player_id in eligible
            ]
            if self._winner_sets_differ(eligible_sets):
                summary['differing'].add(pot_index)
                if summary['second'] is None:
                    witness = self._independent_witness(
                        state, prefix, live_infos, options_by_player,
                        eligible_sets, pot_index, summary)
                    if witness is not None:
                        summary['second'], summary['second_report'] = witness
        if summary['differing'] and summary['second'] is None:
            # The rank-deduped option tables could not construct a feasible
            # witness; the exact enumeration tier keeps a full assignment.
            enumerated = self._aggregate_enumerated(state, prefix)
            if enumerated is not None:
                return enumerated
        return summary

    @staticmethod
    def _winner_sets_differ(eligible_sets):
        """Can the top-ranked eligible players vary across completions?

        The joint maximum is computed only over players eligible for this
        pot: a player excluded by contribution level or folding must not
        influence whether the eligible winner set is fixed.  A single
        eligible player always wins the whole pot regardless of rank.
        """
        if len(eligible_sets) <= 1:
            return False
        maximum = max(max(ranks) for _, ranks in eligible_sets)
        contenders = [
            player_id for player_id, ranks in eligible_sets
            if maximum in ranks
        ]
        forced = [
            player_id for player_id, ranks in eligible_sets
            if ranks == {maximum}
        ]
        # If every contender is forced to the maximum, the winner set (and
        # hence the odd-chip order and award map) is fixed.  Otherwise a
        # contender can fall back to a lower rank and the winner set changes.
        return len(forced) != len(contenders)

    def _independent_witness(self, state, prefix, live_infos,
                             options_by_player, eligible_sets, pot_index,
                             summary):
        """Build a completion whose award map for one pot differs."""
        maximum = max(max(ranks) for _, ranks in eligible_sets)
        # Pick the first contender able to hold a rank below the joint max.
        variable = next(
            player_id for player_id, ranks in eligible_sets
            if maximum in ranks and ranks != {maximum})
        target_map = summary['pot_maps'][pot_index]
        for player_index, options in enumerate(options_by_player):
            if self.players[live_infos[player_index][0]]['id'] != variable:
                continue
            max_options = [
                option for option in options if option[0] == maximum]
            lower_options = [
                option for option in options if option[0] < maximum]
            for lower in lower_options:
                for anchor in max_options or options:
                    assignment = dict(prefix)
                    for other_index, other_options in enumerate(options_by_player):
                        if other_index == player_index:
                            assignment.update(lower[1])
                        else:
                            assignment.update(
                                max(other_options, key=lambda item: item[0])[1])
                    extension = self._extend(frozenset(
                        (slot_id, choices) for slot_id, choices in state
                        if slot_id not in assignment))
                    if extension is None:
                        continue
                    assignment.update(extension)
                    report = self.report(assignment)
                    if dict(report['pots'][pot_index]['awards']) != target_map:
                        return assignment, report
        return None

    # -- adaptive plan ------------------------------------------------------

    @lru_cache(maxsize=None)
    def build_plan(self, state, prefix, depth):
        summary = self.aggregate(state, prefix)
        if summary is None:
            return None
        if not summary['differing']:
            leaf_awards = [
                {'pot': pot['pot'], 'awards': dict(pot['awards'])}
                for pot in summary['template_pots']
            ]
            return 0, {'awards': leaf_awards}

        observed = {slot_id for slot_id, _ in prefix}
        choices = [
            slot_id for slot_id in self.inspectable
            if slot_id in dict(state) and slot_id not in observed
        ]
        used = frozenset(card_value for _, card_value in prefix)
        best = None
        for slot_id in sorted(choices):
            branches = {}
            child_worst = 0
            branch_sizes = []
            for card_value in dict(state)[slot_id]:
                next_state = self._state_after(state, slot_id, card_value)
                next_prefix = tuple(sorted(prefix + ((slot_id, card_value),)))
                # Feasibility and the ability to finish inside the remaining
                # budget are separate properties of a branch.
                if self.aggregate(next_state, next_prefix) is None:
                    continue  # this observation cannot occur
                child = self.build_plan(next_state, next_prefix, depth + 1)
                if child is None:
                    # A real branch that cannot be finished in budget means
                    # this slot cannot guarantee certification; never emit a
                    # tree that only covers part of the possible observations.
                    branches = None
                    break
                child_depth, child_node = child
                branches[card_value] = child_node
                child_worst = max(child_worst, child_depth)
                branch_sizes.append(
                    self.count_completions(next_state, used | {card_value}))
            if not branches:
                continue
            worst = 1 + child_worst
            # Lexicographic policy: minimum worst-case look count, then the
            # slot whose hardest surviving branch leaves the fewest
            # completion classes (maximum worst-case uncertainty reduction),
            # then the finest partition, then the smallest slot id (ASCII).
            cost = (worst, max(branch_sizes), -len(branches), slot_id)
            if best is None or cost < best[0]:
                best = (cost, worst, {'slot_id': slot_id, 'branches': branches})
        if best is None or depth + best[1] > self.budget:
            return None
        return best[1], best[2]

    # -- root report --------------------------------------------------------

    def run(self):
        root_prefix = ()
        if len(self.fixed_cards) != sum(
            1 for location in self._all_locations() if location[0] == 'card'
        ):
            return self._inconsistent()
        root_summary = self.aggregate(self.initial_state, root_prefix)
        if root_summary is None:
            return self._inconsistent()

        pots = []
        for index, pot in enumerate(root_summary['template_pots']):
            if index in root_summary['differing']:
                status = 'ambiguous'
                awards = None
            else:
                status = 'certified'
                awards = dict(root_summary['pot_maps'][index])
            pots.append({
                'pot': pot['pot'],
                'from_exclusive': pot['from_exclusive'],
                'to_inclusive': pot['to_inclusive'],
                'amount': pot['amount'],
                'status': status,
                'awards': awards,
            })

        if root_summary['differing']:
            root_status = 'ambiguous'
            witnesses = [
                {'source': self._build_hand(root_summary['first']),
                 'report': root_summary['first_report']},
                {'source': self._build_hand(root_summary['second']),
                 'report': root_summary['second_report']},
            ]
        else:
            root_status = 'certified'
            witnesses = []

        planned = self.build_plan(self.initial_state, root_prefix, 0)
        plan = planned[1] if planned is not None else None

        return {
            'status': root_status,
            'pots': pots,
            'uncalled_returns': root_summary['first_report']['uncalled_returns'],
            'witnesses': witnesses,
            'plan': plan,
        }


def audit(data):
    parsed = _parse_request(data)
    return _AuditModel(parsed).run()
