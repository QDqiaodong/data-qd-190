"""Persistent cross-hand teaching ledger built on the v1 single-hand rules.

The ledger is a local SQLite database.  Every mutation is committed as one
SQLite transaction, and released statements are stored as immutable JSON
payloads so a later correction cannot rewrite what was already issued.
"""

import json
import os
import sqlite3
import tempfile
from pathlib import Path

from .checks import array, fields, ident, integer
from .service import process

FORMAT = 'showdown-ledger'
SCHEMA_VERSION = 1
MAX_BALANCE = 1_000_000_000
MAX_HAND_NO = 1_000


class LedgerError(ValueError):
    """A malformed or forbidden ledger operation (CLI exit status 2)."""


def _dump(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def _fsync_file(path):
    with open(path, 'rb') as stream:
        os.fsync(stream.fileno())


def _fsync_directory(path):
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _validate_opening(source):
    fields(source, ('opening_balances',))
    balances = source['opening_balances']
    if not isinstance(balances, dict):
        raise LedgerError('opening_balances must be an object')
    if not 2 <= len(balances) <= 100:
        raise LedgerError('opening_balances must contain 2..100 players')
    result = {}
    for player_id, balance in balances.items():
        try:
            ident(player_id)
            integer(balance, 0, MAX_BALANCE)
        except ValueError as error:
            raise LedgerError(f'invalid opening balance for {player_id!r}: {error}') from error
        result[player_id] = balance
    return result


def _initialize(connection, balances):
    connection.executescript(
        """
        CREATE TABLE meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE opening_balances (
            player_id TEXT PRIMARY KEY,
            balance INTEGER NOT NULL
        );
        CREATE TABLE hands (
            hand_no INTEGER PRIMARY KEY,
            revision INTEGER NOT NULL,
            source TEXT NOT NULL
        );
        CREATE TABLE releases (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL UNIQUE,
            through INTEGER NOT NULL,
            payload TEXT NOT NULL
        );
        """
    )
    connection.execute("INSERT INTO meta(key, value) VALUES ('format', ?)", (FORMAT,))
    connection.execute("INSERT INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
    connection.executemany(
        'INSERT INTO opening_balances(player_id, balance) VALUES (?, ?)',
        sorted(balances.items()),
    )
    connection.commit()


def init(state, source):
    """Create a new ledger.  An existing path is never overwritten."""
    balances = _validate_opening(source)
    target = Path(state)
    if os.path.lexists(target):
        raise LedgerError('ledger already exists')
    target.parent.mkdir(parents=True, exist_ok=True)

    descriptor, temporary_name = tempfile.mkstemp(
        dir=str(target.parent), prefix='.ledger-', suffix='.tmp'
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        connection = sqlite3.connect(str(temporary))
        try:
            connection.execute('PRAGMA journal_mode=DELETE')
            connection.execute('PRAGMA synchronous=FULL')
            _initialize(connection, balances)
        finally:
            connection.close()
        _fsync_file(temporary)
        try:
            os.link(temporary, target)
        except FileExistsError as error:
            raise LedgerError('ledger already exists') from error
        _fsync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)
        for suffix in ('-journal', '-wal', '-shm'):
            Path(str(temporary) + suffix).unlink(missing_ok=True)
    return {'head': 0}


def _open(state, writable):
    path = Path(state)
    if not path.is_file():
        raise LedgerError('unknown ledger')
    try:
        with open(path, 'rb') as stream:
            header = stream.read(16)
    except OSError as error:
        raise LedgerError(f'cannot read ledger: {error}') from error
    if header != b'SQLite format 3\x00':
        raise LedgerError('unknown ledger or unsupported format')
    mode = 'rw' if writable else 'ro'
    uri = path.resolve().as_uri() + '?mode=' + mode
    connection = None
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        _validate_schema(connection)
    except (sqlite3.Error, ValueError) as error:
        if connection is not None:
            connection.close()
        raise LedgerError(f'unknown ledger or unsupported format: {error}') from error
    return connection


def _validate_schema(connection):
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    required = {'meta', 'opening_balances', 'hands', 'releases'}
    if not required <= tables:
        raise LedgerError('unknown ledger or unsupported format')
    meta = dict(connection.execute('SELECT key, value FROM meta'))
    if meta.get('format') != FORMAT or meta.get('schema_version') != str(SCHEMA_VERSION):
        raise LedgerError('unknown ledger or unsupported format')


def _load_model(connection):
    """Read the current hands, validating the stored shape as we go."""
    balances = {}
    for player_id, balance in connection.execute(
        'SELECT player_id, balance FROM opening_balances'
    ):
        try:
            ident(player_id)
            integer(balance, 0, MAX_BALANCE)
        except ValueError as error:
            raise LedgerError(f'malformed ledger opening balance: {error}') from error
        balances[player_id] = balance
    if not 2 <= len(balances) <= 100:
        raise LedgerError('malformed ledger roster')

    hands = []
    expected = 1
    for hand_no, revision, raw_source in connection.execute(
        'SELECT hand_no, revision, source FROM hands ORDER BY hand_no'
    ):
        if type(hand_no) is not int or hand_no != expected:
            raise LedgerError('malformed ledger hand sequence')
        if type(revision) is not int or revision < 1:
            raise LedgerError('malformed ledger revision')
        try:
            source = json.loads(raw_source)
        except (TypeError, ValueError) as error:
            raise LedgerError(f'malformed ledger hand {hand_no}: {error}') from error
        if not isinstance(source, dict):
            raise LedgerError(f'malformed ledger hand {hand_no}')
        hands.append({'hand_no': hand_no, 'revision': revision, 'source': source})
        expected += 1
    return {
        'opening_balances': balances,
        'hands': hands,
        'head': hands[-1]['hand_no'] if hands else 0,
    }


def _hand_report(source, hand_no=None):
    try:
        return process(source)
    except (KeyError, TypeError, ValueError, RecursionError) as error:
        where = f' for hand {hand_no}' if hand_no is not None else ''
        raise LedgerError(f'cannot settle input{where}: {error}') from error


def _preview(model):
    current = dict(model['opening_balances'])
    entries = []
    blocking = None
    for hand in model['hands']:
        report = _hand_report(hand['source'], hand['hand_no'])
        shortfalls = []
        for player in report['players']:
            player_id = player['player_id']
            if player_id not in current:
                raise LedgerError(
                    f'hand {hand["hand_no"]} references player outside the roster: {player_id}'
                )
            opening = current[player_id]
            if player['committed'] > opening:
                shortfalls.append({
                    'player_id': player_id,
                    'opening_balance': opening,
                    'committed': player['committed'],
                })
        if shortfalls:
            blocking = {
                'hand_no': hand['hand_no'],
                'revision': hand['revision'],
                'shortfalls': sorted(shortfalls, key=lambda item: item['player_id']),
            }
            break

        closing = dict(current)
        for player in report['players']:
            player_id = player['player_id']
            closing[player_id] = closing[player_id] + player['net']
        entries.append({
            'hand_no': hand['hand_no'],
            'revision': hand['revision'],
            'source': hand['source'],
            'report': report,
            'opening_balances': dict(current),
            'closing_balances': closing,
        })
        current = closing

    return {
        'head': model['head'],
        'revisions': [
            {'hand_no': hand['hand_no'], 'revision': hand['revision']}
            for hand in model['hands']
        ],
        'hands': entries,
        'blocking': blocking,
    }


def preview(state):
    connection = _open(state, False)
    try:
        return _preview(_load_model(connection))
    finally:
        connection.close()


def _validate_apply(source):
    fields(source, ('edits',))
    edits = array(source['edits'], 1, 100)
    seen = set()
    result = []
    for edit in edits:
        fields(edit, ('hand_no', 'expected_revision', 'source'))
        hand_no = integer(edit['hand_no'], 1, MAX_HAND_NO)
        expected = integer(edit['expected_revision'], 0, 2 ** 63 - 1)
        if hand_no in seen:
            raise LedgerError(f'duplicate hand_no in batch: {hand_no}')
        seen.add(hand_no)
        result.append((hand_no, expected, edit['source']))
    return result


def apply(state, source):
    edits = _validate_apply(source)
    connection = _open(state, True)
    try:
        connection.execute('BEGIN IMMEDIATE')
        model = _load_model(connection)
        existing = {hand['hand_no']: hand for hand in model['hands']}
        head = model['head']

        for hand_no, expected, _ in edits:
            if hand_no in existing:
                if expected == 0:
                    raise LedgerError(
                        f'hand {hand_no}: expected_revision 0 is only valid for a new hand'
                    )
                if expected != existing[hand_no]['revision']:
                    raise LedgerError(
                        f'hand {hand_no}: expected revision {expected}, '
                        f'current revision {existing[hand_no]["revision"]}'
                    )
            elif expected != 0:
                raise LedgerError(
                    f'hand {hand_no}: new hand requires expected_revision 0'
                )

        new_numbers = sorted(hand_no for hand_no, expected, _ in edits if expected == 0)
        expected_new = list(range(head + 1, head + 1 + len(new_numbers)))
        if new_numbers != expected_new:
            wanted = ', '.join(str(number) for number in expected_new) or 'none'
            raise LedgerError(
                f'new hand numbers must be consecutive after hand {head}; expected {wanted}'
            )

        roster = set(model['opening_balances'])
        for hand_no, _, hand_source in edits:
            report = _hand_report(hand_source, hand_no)
            unknown = sorted({player['player_id'] for player in report['players']} - roster)
            if unknown:
                raise LedgerError(
                    f'hand {hand_no}: player(s) outside roster: {", ".join(unknown)}'
                )

        revisions = []
        for hand_no, _, hand_source in sorted(edits, key=lambda item: item[0]):
            if hand_no in existing:
                revision = existing[hand_no]['revision'] + 1
                connection.execute(
                    'UPDATE hands SET revision=?, source=? WHERE hand_no=?',
                    (revision, _dump(hand_source), hand_no),
                )
            else:
                revision = 1
                connection.execute(
                    'INSERT INTO hands(hand_no, revision, source) VALUES (?, ?, ?)',
                    (hand_no, revision, _dump(hand_source)),
                )
            revisions.append({'hand_no': hand_no, 'revision': revision})
        connection.commit()
        return {'head': head + len(new_numbers), 'revisions': revisions}
    except BaseException:
        try:
            connection.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        connection.close()


def _release_payloads(connection):
    payloads = []
    for raw in connection.execute('SELECT payload FROM releases ORDER BY seq'):
        try:
            payload = json.loads(raw[0])
        except (TypeError, ValueError) as error:
            raise LedgerError(f'malformed stored release: {error}') from error
        payloads.append(payload)
    return payloads


def _net_map(hands):
    result = {}
    for entry in hands:
        result[entry['hand_no']] = {
            player['player_id']: player['net'] for player in entry['report']['players']
        }
    return result


def release(state, request_id, through):
    ident(request_id)
    integer(through, 1, MAX_HAND_NO)
    connection = _open(state, True)
    try:
        connection.execute('BEGIN IMMEDIATE')

        # A retried request returns the stored statement before any current
        # eligibility or shortening checks are re-run.
        row = connection.execute(
            'SELECT payload FROM releases WHERE request_id=?', (request_id,)
        ).fetchone()
        if row is not None:
            payload = json.loads(row[0])
            if payload.get('through') != through:
                raise LedgerError(
                    f'release {request_id} was already issued through '
                    f'{payload.get("through")}, not {through}'
                )
            connection.rollback()
            return payload

        model = _load_model(connection)
        stored = _release_payloads(connection)
        previous = stored[-1] if stored else None
        if previous is not None and through < previous['through']:
            raise LedgerError(
                f'through {through} is shorter than the last release '
                f'({previous["request_id"]} through {previous["through"]})'
            )

        state_view = _preview(model)
        usable = state_view['hands']
        if through > len(usable):
            blocking = state_view['blocking']
            if blocking is not None:
                detail = (
                    f'hand {blocking["hand_no"]} revision {blocking["revision"]} '
                    'has insufficient opening balances'
                )
            else:
                detail = f'only {len(usable)} hand(s) are usable'
            raise LedgerError(
                f'through {through} exceeds the usable prefix ({detail})'
            )
        hands = usable[:through]

        old_nets = _net_map(previous['hands']) if previous is not None else {}
        new_nets = _net_map(hands)
        postings = []
        hand_totals = {}
        for hand_no in sorted(set(range(1, through + 1)) | set(old_nets)):
            old = old_nets.get(hand_no, {})
            new = new_nets.get(hand_no, {})
            for player_id in sorted(set(old) | set(new)):
                delta = new.get(player_id, 0) - old.get(player_id, 0)
                if delta:
                    postings.append({
                        'hand_no': hand_no,
                        'player_id': player_id,
                        'delta': delta,
                    })
                    hand_totals[hand_no] = hand_totals.get(hand_no, 0) + delta
        if any(total != 0 for total in hand_totals.values()):
            raise LedgerError('internal postings imbalance')

        closing = hands[-1]['closing_balances']
        cumulative = dict(model['opening_balances'])
        for payload in stored:
            for posting in payload['postings']:
                cumulative[posting['player_id']] += posting['delta']
        for posting in postings:
            cumulative[posting['player_id']] += posting['delta']
        if cumulative != closing:
            raise LedgerError('internal ledger balance mismatch')

        payload = {
            'request_id': request_id,
            'through': through,
            'previous_request_id': previous['request_id'] if previous else None,
            'hands': hands,
            'closing_balances': closing,
            'postings': postings,
        }
        connection.execute(
            'INSERT INTO releases(request_id, through, payload) VALUES (?, ?, ?)',
            (request_id, through, _dump(payload)),
        )
        connection.commit()
        return payload
    except BaseException:
        try:
            connection.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        connection.close()


def read_release(state, request_id):
    ident(request_id)
    connection = _open(state, False)
    try:
        row = connection.execute(
            'SELECT payload FROM releases WHERE request_id=?', (request_id,)
        ).fetchone()
        if row is None:
            raise LedgerError(f'unknown release: {request_id}')
        try:
            return json.loads(row[0])
        except (TypeError, ValueError) as error:
            raise LedgerError(f'malformed stored release: {error}') from error
    finally:
        connection.close()
