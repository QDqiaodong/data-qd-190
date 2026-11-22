"""Reconcile two independent exports of the same teaching-table hands.

The left export carries complete v1 hand inputs; the right export carries only
the per-player net chips that were actually transcribed.  The two sources may
each miss hands, a shared short table tag may repeat, and the devices' clocks
are not synchronised.  A shared alignment uses one integer clock offset for the
whole batch; matched records must carry the same tag, their timestamps must
agree with that offset within the tolerance, and matched right records must be
strictly increasing in ``seq`` when the left records are visited in ``seq``
order.  Every anchor pair supplied by the staff must appear in the alignment.

All offsets that reach the maximum pair count are reported, including the
empty offsets between them.  Chip differences are produced only for pairs that
are forced in every maximum alignment, so an ambiguous match can never turn
into a wrong reconciliation item.

The offset sweep only visits the boundaries introduced by candidate pairs
(``right.ts-left.ts +/- tolerance``); it never walks the numeric offset window
one millisecond at a time.
"""
from .checks import array, fields, ident, integer, unique
from .service import process


def _no_alignment():
    return {
        'status': 'no_alignment',
        'max_pairs': None,
        'offsets': [],
        'left': [],
        'right': [],
        'reconciled': [],
        'examples': [],
    }


def _parse(data):
    fields(data, ('left', 'right', 'clock', 'anchors'), ('focus_left_id',))

    left_records = []
    for item in array(data['left'], 0, 100):
        fields(item, ('id', 'seq', 'ts', 'tag', 'hand'))
        record = {
            'id': ident(item['id']),
            'seq': integer(item['seq'], 1, 1_000_000_000),
            'ts': integer(item['ts'], 0, 1_000_000_000_000),
            'tag': ident(item['tag']),
            'hand': item['hand'],
        }
        # Validate and settle every complete v1 hand, even a hand that will
        # end up unmatched.  The settled per-player net is what the left
        # source says *should* have been recorded.
        report = process(item['hand'])
        record['expected'] = {
            player['player_id']: player['net'] for player in report['players']
        }
        left_records.append(record)
    unique([record['id'] for record in left_records])
    unique([record['seq'] for record in left_records])
    left_records.sort(key=lambda record: record['seq'])

    right_records = []
    for item in array(data['right'], 0, 100):
        fields(item, ('id', 'seq', 'ts', 'tag', 'net'))
        observed = item['net']
        if not isinstance(observed, dict) or len(observed) > 9:
            raise ValueError('net must map at most 9 player ids to integers')
        checked = {}
        for player_id, value in observed.items():
            checked[ident(player_id)] = integer(value, -9_000_000_000, 9_000_000_000)
        right_records.append({
            'id': ident(item['id']),
            'seq': integer(item['seq'], 1, 1_000_000_000),
            'ts': integer(item['ts'], 0, 1_000_000_000_000),
            'tag': ident(item['tag']),
            'net': checked,
        })
    unique([record['id'] for record in right_records])
    unique([record['seq'] for record in right_records])
    right_records.sort(key=lambda record: record['seq'])

    clock = data['clock']
    fields(clock, ('min_offset', 'max_offset', 'tolerance'))
    min_offset = integer(clock['min_offset'], -1_000_000_000_000, 1_000_000_000_000)
    max_offset = integer(clock['max_offset'], -1_000_000_000_000, 1_000_000_000_000)
    if min_offset > max_offset:
        raise ValueError('clock min_offset exceeds max_offset')
    tolerance = integer(clock['tolerance'], 0, 1_000_000)

    left_index = {record['id']: index for index, record in enumerate(left_records)}
    right_index = {record['id']: index for index, record in enumerate(right_records)}

    anchors = []
    for item in array(data['anchors'], 0, 100):
        fields(item, ('left_id', 'right_id'))
        left_id = ident(item['left_id'])
        right_id = ident(item['right_id'])
        if left_id not in left_index:
            raise ValueError(f'anchor references unknown left id: {left_id}')
        if right_id not in right_index:
            raise ValueError(f'anchor references unknown right id: {right_id}')
        anchors.append((left_index[left_id], right_index[right_id]))
    unique([left for left, _ in anchors])
    unique([right for _, right in anchors])
    anchors.sort(key=lambda pair: pair[0])

    focus = None
    if 'focus_left_id' in data:
        focus_id = ident(data['focus_left_id'])
        if focus_id not in left_index:
            raise ValueError(f'focus_left_id references unknown left id: {focus_id}')
        focus = left_index[focus_id]

    return {
        'left': left_records,
        'right': right_records,
        'min_offset': min_offset,
        'max_offset': max_offset,
        'tolerance': tolerance,
        'anchors': anchors,
        'focus': focus,
    }


def _rectangles(anchors, left_count, right_count):
    """Split the alignable area around the required anchor pairs."""
    rectangles = []
    row_start = 0
    column_start = 0
    for left, right in anchors:
        rectangles.append((row_start, left, column_start, right))
        row_start = left + 1
        column_start = right + 1
    rectangles.append((row_start, left_count, column_start, right_count))
    return rectangles


def _scope(parsed):
    """Return (L, U) or None when the anchors cannot share one alignment."""
    left = parsed['left']
    right = parsed['right']
    tolerance = parsed['tolerance']
    low = parsed['min_offset']
    high = parsed['max_offset']
    for anchor_left, anchor_right in parsed['anchors']:
        left_record = left[anchor_left]
        right_record = right[anchor_right]
        if left_record['tag'] != right_record['tag']:
            return None
        difference = right_record['ts'] - left_record['ts']
        low = max(low, difference - tolerance)
        high = min(high, difference + tolerance)
    previous_right = -1
    for _, anchor_right in parsed['anchors']:
        if anchor_right <= previous_right:
            return None
        previous_right = anchor_right
    if low > high:
        return None
    return low, high


def _segment_max(state, rectangles, anchor_count):
    """Maximum ordered matching for one constant edge set."""
    total = anchor_count
    for row_start, row_stop, column_start, column_stop in rectangles:
        width = column_stop - column_start
        if row_start == row_stop or width == 0:
            continue
        column_mask = (1 << width) - 1
        bits = 0
        for row in range(row_start, row_stop):
            edges = ((state[row] >> column_start) & column_mask) | bits
            shifted = (bits << 1) | 1
            bits = edges & ~(edges - shifted)
        total += bits.bit_count()
    return total


def _tables(rows, row_count, column_count):
    """Prefix and suffix LCS tables for one rectangle."""
    prefix = [[0] * (column_count + 1) for _ in range(row_count + 1)]
    for row in range(1, row_count + 1):
        edges = rows[row - 1]
        above = prefix[row - 1]
        current = prefix[row]
        bit = 1
        for column in range(1, column_count + 1):
            if edges & bit:
                best = above[column - 1] + 1
            else:
                best = 0
            if above[column] > best:
                best = above[column]
            if current[column - 1] > best:
                best = current[column - 1]
            current[column] = best
            bit <<= 1
    suffix = [[0] * (column_count + 1) for _ in range(row_count + 1)]
    for row in range(row_count - 1, -1, -1):
        edges = rows[row]
        below = suffix[row + 1]
        current = suffix[row]
        for column in range(column_count - 1, -1, -1):
            if edges & (1 << column):
                best = below[column + 1] + 1
            else:
                best = 0
            if below[column] > best:
                best = below[column]
            if current[column + 1] > best:
                best = current[column + 1]
            current[column] = best
    return prefix, suffix, prefix[row_count][column_count]


def _align(rows, column_count):
    """Deterministically reconstruct one maximum ordered matching."""
    row_count = len(rows)
    if row_count == 0 or column_count == 0:
        return []
    table = [[0] * (column_count + 1) for _ in range(row_count + 1)]
    for row in range(1, row_count + 1):
        edges = rows[row - 1]
        above = table[row - 1]
        current = table[row]
        bit = 1
        for column in range(1, column_count + 1):
            if edges & bit:
                best = above[column - 1] + 1
            else:
                best = 0
            if above[column] > best:
                best = above[column]
            if current[column - 1] > best:
                best = current[column - 1]
            current[column] = best
            bit <<= 1
    pairs = []
    row = row_count
    column = column_count
    while row and column:
        if rows[row - 1] & (1 << (column - 1)) \
                and table[row][column] == table[row - 1][column - 1] + 1:
            pairs.append((row - 1, column - 1))
            row -= 1
            column -= 1
        elif table[row - 1][column] >= table[row][column - 1]:
            row -= 1
        else:
            column -= 1
    pairs.reverse()
    return pairs


def _masked_rows(state, rectangle):
    row_start, row_stop, column_start, column_stop = rectangle
    width = column_stop - column_start
    if row_start == row_stop or width == 0:
        return [], 0
    column_mask = (1 << width) - 1
    return ([((state[row] >> column_start) & column_mask)
             for row in range(row_start, row_stop)], width)


def _aligned_pairs(state, rectangles, anchors, focus, outcome):
    """Build one complete maximum alignment, optionally forcing focus."""
    pairs = list(anchors)
    for rectangle in rectangles:
        row_start, row_stop, column_start, column_stop = rectangle
        rows, width = _masked_rows(state, rectangle)
        row_count = len(rows)
        if row_count == 0 or width == 0:
            continue
        focus_local = None
        if focus is not None and row_start <= focus < row_stop:
            focus_local = focus - row_start
        if focus_local is None:
            local = _align(rows, width)
            source_rows = range(row_count)
        elif outcome[0] == 'match':
            column_local = outcome[1] - column_start
            prefix_width = column_local
            prefix_rows = [rows[row] & ((1 << prefix_width) - 1)
                           for row in range(focus_local)]
            local = _align(prefix_rows, prefix_width)
            suffix_width = width - column_local - 1
            suffix_rows = [rows[row] >> (column_local + 1)
                           for row in range(focus_local + 1, row_count)]
            for row, column in _align(suffix_rows, suffix_width):
                local.append((row + focus_local + 1, column + column_local + 1))
            local.append((focus_local, column_local))
            source_rows = range(row_count)
        else:  # focus is deliberately unmatched in this witness
            kept_rows = rows[:focus_local] + rows[focus_local + 1:]
            local = _align(kept_rows, width)
            source_rows = [
                row for row in range(row_count) if row != focus_local
            ]
        for local_row, local_column in local:
            pairs.append((row_start + source_rows[local_row],
                          column_start + local_column))
    pairs.sort()
    return pairs


def reconcile(data):
    parsed = _parse(data)
    left = parsed['left']
    right = parsed['right']
    left_count = len(left)
    right_count = len(right)
    tolerance = parsed['tolerance']
    anchors = parsed['anchors']
    focus = parsed['focus']

    scope = _scope(parsed)
    if scope is None:
        return _no_alignment()
    low, high = scope

    rectangles = _rectangles(anchors, left_count, right_count)
    row_rectangle = [-1] * left_count
    column_rectangle = [-1] * right_count
    for index, (row_start, row_stop, column_start, column_stop) in enumerate(rectangles):
        for row in range(row_start, row_stop):
            row_rectangle[row] = index
        for column in range(column_start, column_stop):
            column_rectangle[column] = index

    # Only pairs inside the same anchor rectangle can coexist with the anchors.
    events = {}

    def event(where, row, bit):
        events.setdefault(where, []).append((row, bit))

    for row in range(left_count):
        rectangle = row_rectangle[row]
        if rectangle < 0:
            continue
        left_tag = left[row]['tag']
        left_ts = left[row]['ts']
        for column in range(right_count):
            if column_rectangle[column] != rectangle:
                continue
            right_record = right[column]
            if right_record['tag'] != left_tag:
                continue
            difference = right_record['ts'] - left_ts
            start = max(low, difference - tolerance)
            stop = min(high, difference + tolerance)
            if start > stop:
                continue
            bit = 1 << column
            event(start, row, bit)
            event(stop + 1, row, bit)

    coordinates = sorted(set([low, high + 1]) | set(events))
    state = [0] * left_count
    segments = []
    for index, coordinate in enumerate(coordinates):
        for row, bit in events.get(coordinate, ()):
            state[row] ^= bit
        if coordinate > high or index + 1 == len(coordinates):
            break
        following = coordinates[index + 1]
        segments.append((coordinate, following - 1,
                         _segment_max(state, rectangles, len(anchors))))

    best = max(segment[2] for segment in segments)

    # Every integer offset reaching the batch maximum, holes preserved.
    offset_ranges = []
    for start, stop, value in segments:
        if value != best:
            continue
        if offset_ranges and offset_ranges[-1][1] + 1 == start:
            offset_ranges[-1] = (offset_ranges[-1][0], stop)
        else:
            offset_ranges.append((start, stop))

    left_partners = [set() for _ in range(left_count)]
    right_partners = [set() for _ in range(right_count)]
    left_unmatched = [False] * left_count
    right_unmatched = [False] * right_count
    for anchor_left, anchor_right in anchors:
        left_partners[anchor_left].add(anchor_right)
        right_partners[anchor_right].add(anchor_left)

    match_support = {}
    unmatched_support = {}

    state = [0] * left_count
    segment_index = 0
    for index, coordinate in enumerate(coordinates):
        for row, bit in events.get(coordinate, ()):
            state[row] ^= bit
        if coordinate > high or index + 1 == len(coordinates):
            break
        start, stop, value = segments[segment_index]
        segment_index += 1
        if value != best:
            continue
        for rectangle in rectangles:
            row_start, row_stop, column_start, column_stop = rectangle
            if row_stop == row_start:
                for column in range(column_start, column_stop):
                    right_unmatched[column] = True
                continue
            if column_stop == column_start:
                for row in range(row_start, row_stop):
                    left_unmatched[row] = True
                    if focus is not None and row == focus:
                        unmatched_support.setdefault(row, start)
                continue
            rows, width = _masked_rows(state, rectangle)
            row_count = len(rows)
            # Columns and rows without any active edge can never be matched
            # in this segment; dropping them keeps the tables proportional to
            # the evidence actually present instead of the batch size.
            union_mask = 0
            for edges in rows:
                union_mask |= edges
            active_columns = []
            while union_mask:
                lowest = union_mask & -union_mask
                active_columns.append(lowest.bit_length() - 1)
                union_mask ^= lowest
            active_rows = [
                row for row, edges in enumerate(rows)
                if edges
            ]
            if not active_rows or not active_columns:
                for row in range(row_count):
                    left_unmatched[row_start + row] = True
                    if focus is not None and row_start + row == focus:
                        unmatched_support.setdefault(row_start + row, start)
                for column in range(width):
                    right_unmatched[column_start + column] = True
                continue
            compact_index = {
                column: index for index, column in enumerate(active_columns)
            }
            compressed = []
            for row in active_rows:
                packed = 0
                edges = rows[row]
                while edges:
                    lowest = edges & -edges
                    packed |= 1 << compact_index[lowest.bit_length() - 1]
                    edges ^= lowest
                compressed.append(packed)
            compact_width = len(active_columns)
            prefix, suffix, rectangle_best = _tables(
                compressed, len(active_rows), compact_width)
            for compact_row, edges in enumerate(compressed):
                row = active_rows[compact_row]
                while edges:
                    lowest = edges & -edges
                    column = lowest.bit_length() - 1
                    if prefix[compact_row][column] + 1 \
                            + suffix[compact_row + 1][column + 1] == rectangle_best:
                        global_left = row_start + row
                        global_right = column_start + active_columns[column]
                        left_partners[global_left].add(global_right)
                        right_partners[global_right].add(global_left)
                        if focus is not None and global_left == focus:
                            match_support.setdefault(global_right, start)
                    edges ^= lowest
            active_row_set = set(active_rows)
            for row in range(row_count):
                if row in active_row_set:
                    continue
                left_unmatched[row_start + row] = True
                if focus is not None and row_start + row == focus:
                    unmatched_support.setdefault(row_start + row, start)
            for compact_row, row in enumerate(active_rows):
                prefix_row = prefix[compact_row]
                suffix_row = suffix[compact_row + 1]
                for boundary in range(compact_width + 1):
                    if prefix_row[boundary] + suffix_row[boundary] == rectangle_best:
                        left_unmatched[row_start + row] = True
                        if focus is not None and row_start + row == focus:
                            unmatched_support.setdefault(row_start + row, start)
                        break
            for column in range(width):
                compact_column = compact_index.get(column)
                if compact_column is None:
                    right_unmatched[column_start + column] = True
                    continue
                for boundary in range(len(active_rows) + 1):
                    if prefix[boundary][compact_column] \
                            + suffix[boundary][compact_column + 1] == rectangle_best:
                        right_unmatched[column_start + column] = True
                        break

    # Confirmed pairs: exactly one partner and no maximum alignment omits them.
    reconciled = []
    for row in range(left_count):
        partners = left_partners[row]
        if len(partners) == 1 and not left_unmatched[row]:
            column = next(iter(partners))
            expected = left[row]['expected']
            observed = right[column]['net']
            differences = []
            for player_id in sorted(set(expected) | set(observed)):
                expected_value = expected.get(player_id)
                observed_value = observed.get(player_id)
                if expected_value != observed_value:
                    differences.append({
                        'player_id': player_id,
                        'expected': expected_value,
                        'observed': observed_value,
                    })
            reconciled.append({
                'left_id': left[row]['id'],
                'right_id': right[column]['id'],
                'differences': differences,
            })

    outcomes = []
    if focus is not None:
        for column in sorted(left_partners[focus]):
            outcomes.append(('match', column))
        if left_unmatched[focus]:
            outcomes.append(('unmatched', None))
    examples = []
    if len(outcomes) >= 2:
        for kind, column in outcomes[:2]:
            if kind == 'match':
                # An anchor pair is present in every alignment; its support
                # was guaranteed by _scope, so any maximum offset works.
                witness_offset = match_support.get(
                    column, offset_ranges[0][0])
            else:
                witness_offset = unmatched_support[focus]
            witness_state = [0] * left_count
            for row in range(left_count):
                rectangle = row_rectangle[row]
                if rectangle < 0:
                    continue
                for other in range(right_count):
                    if column_rectangle[other] != rectangle:
                        continue
                    if right[other]['tag'] != left[row]['tag']:
                        continue
                    if abs((right[other]['ts'] - left[row]['ts']) - witness_offset) \
                            <= tolerance:
                        witness_state[row] |= 1 << other
            pairs = _aligned_pairs(
                witness_state, rectangles, anchors, focus, (kind, column))
            examples.append({
                'offset': witness_offset,
                'pairs': [
                    {'left_id': left[row]['id'], 'right_id': right[column]['id']}
                    for row, column in pairs
                ],
            })

    left_report = []
    for row, record in enumerate(left):
        left_report.append({
            'id': record['id'],
            'partners': [right[column]['id']
                         for column in sorted(left_partners[row])],
            'may_be_unmatched': bool(left_unmatched[row]),
        })
    right_report = []
    for column, record in enumerate(right):
        right_report.append({
            'id': record['id'],
            'partners': [left[row]['id']
                         for row in sorted(right_partners[column])],
            'may_be_unmatched': bool(right_unmatched[column]),
        })

    return {
        'status': 'ok',
        'max_pairs': best,
        'offsets': [{'from': start, 'to': stop} for start, stop in offset_ranges],
        'left': left_report,
        'right': right_report,
        'reconciled': reconciled,
        'examples': examples,
    }
