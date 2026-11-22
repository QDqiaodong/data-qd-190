"""Revocable teaching-hand sample corpus with an offline public package.

The corpus is the private half of the teaching-exercise pipeline.  Staff
record settled hands as samples (``sample_id, lesson_id, table_id, hand``)
and maintain per-player consent as permissions.  A sample is publishable
only while every participant of its hand currently consents — including
folded players, players who won nothing and players who committed zero.
A lesson appears only while its publishable samples span at least two
different source tables; the whole corpus is re-judged after every batch,
so later consent changes also re-judge samples recorded earlier.  Samples
are never rewritten and a used ``sample_id`` is never accepted again.

The distribution side reads exactly one JSON file at the fixed public
path recorded at ``init`` time; it never touches the private state.
Every accepted batch installs a new generation atomically (temp file,
fsync, rename), so a reader always sees either the previous or the
complete new generation.  The private database commits the batch together
with the rendered package *before* the rename; ``recover`` re-publishes
the committed generation when the maintenance process died in between
and never invents a new generation.

The public package carries only seat-based settlement projections and
their digests: no hole cards or best-five evidence, no player ids, no
table ids, no private paths and no consent data.  Anonymity is not
promised and files already copied by others are not recalled.
"""

import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path

from .checks import array, fields, ident
from .service import process

FORMAT = 'showdown-corpus'
SCHEMA_VERSION = 1
MAX_SAMPLES_PER_BATCH = 100
MAX_PERMISSIONS_PER_BATCH = 1000
MAX_SAMPLES = 2000
MIN_TABLES_PER_LESSON = 2
SQLITE_HEADER = b'SQLite format 3\x00'
TEMP_PREFIX = '.corpus-public-'


class CorpusError(ValueError):
    """A malformed or forbidden corpus operation (CLI exit status 2)."""


def _canonical(value):
    """The digest/package encoding: sorted keys, no whitespace, UTF-8."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _digest(settlement):
    """SHA-256 of the canonical settlement encoding, lowercase hex."""
    return hashlib.sha256(_canonical(settlement).encode('utf-8')).hexdigest()


def _project(hand, report):
    """Reduce a settled v1 hand to its public seat-based settlement object.

    Player ids, table ids and all card evidence are dropped here; only
    seats, flags, integer chips and v1 ordering survive.  Reordering the
    input players array or renaming the players therefore yields the same
    object and the same digest.
    """
    by_id = {player['id']: player for player in hand['players']}
    players = []
    for entry in sorted(report['players'], key=lambda player: player['seat']):
        source = by_id[entry['player_id']]
        players.append({
            'seat': entry['seat'],
            'folded': source['folded'],
            'committed': entry['committed'],
            'net': entry['net'],
        })
    pots = []
    for pot in report['pots']:
        pots.append({
            'pot': pot['pot'],
            'amount': pot['amount'],
            'eligible_seats': [by_id[player_id]['seat']
                               for player_id in pot['eligible_ids']],
            'winner_seats_in_odd_chip_order': [
                by_id[player_id]['seat']
                for player_id in pot['winner_ids_in_odd_chip_order']],
            'awards': {str(by_id[player_id]['seat']): amount
                       for player_id, amount in pot['awards'].items()},
        })
    returns = [{
        'seat': by_id[item['player_id']]['seat'],
        'from_exclusive': item['from_exclusive'],
        'to_inclusive': item['to_inclusive'],
        'amount': item['amount'],
    } for item in report['uncalled_returns']]
    return {
        'button_seat': hand['button_seat'],
        'players': players,
        'pots': pots,
        'uncalled_returns': returns,
    }


def _validate_batch(source):
    """Check one apply batch without touching any state.

    Every hand is settled under the unchanged v1 rules, even one that can
    never be published; a hand that does not settle rejects the batch.
    """
    fields(source, ('samples', 'permissions'))
    raw_samples = array(source['samples'], 0, MAX_SAMPLES_PER_BATCH)
    raw_permissions = array(source['permissions'], 0, MAX_PERMISSIONS_PER_BATCH)
    if not raw_samples and not raw_permissions:
        raise CorpusError('batch must contain samples or permissions')

    samples = []
    seen_samples = set()
    for raw in raw_samples:
        fields(raw, ('sample_id', 'lesson_id', 'table_id', 'hand'))
        try:
            sample_id = ident(raw['sample_id'])
            lesson_id = ident(raw['lesson_id'])
            table_id = ident(raw['table_id'])
        except ValueError as error:
            raise CorpusError(f'invalid sample identity: {error}') from error
        if sample_id in seen_samples:
            raise CorpusError(f'duplicate sample_id in batch: {sample_id}')
        seen_samples.add(sample_id)
        hand = raw['hand']
        try:
            report = process(hand)
        except (KeyError, TypeError, ValueError, RecursionError) as error:
            raise CorpusError(
                f'sample {sample_id}: hand does not settle under v1: {error}'
            ) from error
        settlement = _project(hand, report)
        samples.append({
            'sample_id': sample_id,
            'lesson_id': lesson_id,
            'table_id': table_id,
            'participants': [player['player_id'] for player in report['players']],
            'settlement': settlement,
            'digest': _digest(settlement),
        })

    permissions = []
    seen_players = set()
    for raw in raw_permissions:
        fields(raw, ('player_id', 'allowed'))
        try:
            player_id = ident(raw['player_id'])
        except ValueError as error:
            raise CorpusError(f'invalid permission player_id: {error}') from error
        if type(raw['allowed']) is not bool:
            raise CorpusError('permission allowed must be boolean')
        if player_id in seen_players:
            raise CorpusError(f'duplicate player_id in batch: {player_id}')
        seen_players.add(player_id)
        permissions.append((player_id, raw['allowed']))
    return samples, permissions


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


def _write_temp(directory, payload):
    """One I/O boundary: a fully written and synced sibling temp file."""
    descriptor, name = tempfile.mkstemp(
        dir=str(directory), prefix=TEMP_PREFIX, suffix='.tmp')
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return Path(name)


def _rename(source, target):
    """One I/O boundary: the atomic rename that makes a generation visible."""
    os.replace(source, target)


def _publish(path, payload):
    """Replace the fixed public path with a complete new generation."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = _write_temp(path.parent, payload)
    try:
        _rename(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _create_public(path, payload):
    """Create the generation-0 public file; never overwrite an existing one."""
    temporary = _write_temp(Path(path).parent, payload)
    try:
        os.link(temporary, path)
    except FileExistsError as error:
        raise CorpusError('public file already exists') from error
    finally:
        temporary.unlink(missing_ok=True)
    _fsync_directory(Path(path).parent)


def _remove_stray_temps(directory, keep):
    """Best-effort cleanup of interrupted publication temp files."""
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        if not name.startswith(TEMP_PREFIX):
            continue
        path = os.path.join(directory, name)
        if os.path.abspath(path) in keep:
            continue
        try:
            os.unlink(path)
        except OSError:
            pass


def _empty_package(generation):
    return {'generation': generation, 'lessons': {}, 'objects': {}}


def _initialize(connection, public_path):
    connection.executescript(
        """
        CREATE TABLE meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE samples (
            sample_id TEXT PRIMARY KEY,
            lesson_id TEXT NOT NULL,
            table_id TEXT NOT NULL,
            participants TEXT NOT NULL,
            digest TEXT NOT NULL
        );
        CREATE TABLE objects (
            digest TEXT PRIMARY KEY,
            payload TEXT NOT NULL
        );
        CREATE TABLE permissions (
            player_id TEXT PRIMARY KEY,
            allowed INTEGER NOT NULL
        );
        """
    )
    meta = {
        'format': FORMAT,
        'schema_version': str(SCHEMA_VERSION),
        'public_path': public_path,
        'generation': '0',
        'package': _canonical(_empty_package(0)),
    }
    connection.executemany(
        'INSERT INTO meta(key, value) VALUES (?, ?)', sorted(meta.items()))
    connection.commit()


def init(state, public):
    """Create an empty corpus and its generation-0 public package.

    Neither path may exist yet and the two paths must differ; a failed
    init removes the half-created files it just made, nothing else.
    """
    state_path = Path(state)
    public_path = Path(public)
    if os.path.abspath(state_path) == os.path.abspath(public_path):
        raise CorpusError('private state and public file must be different paths')
    if os.path.lexists(state_path):
        raise CorpusError('corpus state already exists')
    if os.path.lexists(public_path):
        raise CorpusError('public file already exists')
    state_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.parent.mkdir(parents=True, exist_ok=True)
    absolute_public = os.path.abspath(public_path)

    descriptor, temporary_name = tempfile.mkstemp(
        dir=str(state_path.parent), prefix='.corpus-', suffix='.tmp')
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        connection = sqlite3.connect(str(temporary))
        try:
            connection.execute('PRAGMA journal_mode=DELETE')
            connection.execute('PRAGMA synchronous=FULL')
            _initialize(connection, absolute_public)
        finally:
            connection.close()
        _fsync_file(temporary)
        try:
            os.link(temporary, state_path)
        except FileExistsError as error:
            raise CorpusError('corpus state already exists') from error
        _fsync_directory(state_path.parent)
    finally:
        temporary.unlink(missing_ok=True)
        for suffix in ('-journal', '-wal', '-shm'):
            Path(str(temporary) + suffix).unlink(missing_ok=True)
    try:
        _create_public(absolute_public, _canonical(_empty_package(0)).encode('utf-8'))
    except BaseException:
        # Roll back the half-initialised corpus: only the state file this
        # invocation just linked is removed.  A process killed before this
        # point can be completed later by ``recover``.
        state_path.unlink(missing_ok=True)
        raise
    return {'generation': 0, 'public': absolute_public}


def _open(state, writable):
    path = Path(state)
    if not path.is_file():
        raise CorpusError('unknown corpus')
    try:
        with open(path, 'rb') as stream:
            header = stream.read(16)
    except OSError as error:
        raise CorpusError(f'cannot read corpus: {error}') from error
    if header != SQLITE_HEADER:
        raise CorpusError('unknown corpus or unsupported format')
    mode = 'rw' if writable else 'ro'
    uri = path.resolve().as_uri() + '?mode=' + mode
    connection = None
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        if writable:
            connection.execute('PRAGMA synchronous=FULL')
        _validate_schema(connection)
    except (sqlite3.Error, ValueError) as error:
        if connection is not None:
            connection.close()
        raise CorpusError(f'unknown corpus or unsupported format: {error}') from error
    return connection


def _validate_schema(connection):
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'meta', 'samples', 'objects', 'permissions'} <= tables:
        raise CorpusError('unknown corpus or unsupported format')
    meta = dict(connection.execute('SELECT key, value FROM meta'))
    if meta.get('format') != FORMAT \
            or meta.get('schema_version') != str(SCHEMA_VERSION):
        raise CorpusError('unknown corpus or unsupported format')


def _load_model(connection):
    """Read the whole private corpus, validating the stored shape."""
    meta = dict(connection.execute('SELECT key, value FROM meta'))
    if not {'public_path', 'generation', 'package'} <= set(meta):
        raise CorpusError('malformed corpus metadata')
    try:
        generation = int(meta['generation'])
    except ValueError as error:
        raise CorpusError('malformed corpus generation') from error
    if generation < 0:
        raise CorpusError('malformed corpus generation')
    try:
        package = json.loads(meta['package'])
    except ValueError as error:
        raise CorpusError(f'malformed stored package: {error}') from error
    if not isinstance(package, dict) or package.get('generation') != generation:
        raise CorpusError('malformed stored package')

    samples = []
    for row in connection.execute(
        'SELECT sample_id, lesson_id, table_id, participants, digest'
        ' FROM samples ORDER BY sample_id'
    ):
        sample_id, lesson_id, table_id, raw_participants, digest = row
        try:
            ident(sample_id)
            ident(lesson_id)
            ident(table_id)
            participants = json.loads(raw_participants)
            if not isinstance(participants, list) or not participants:
                raise ValueError('participants must be a non-empty array')
            for player_id in participants:
                ident(player_id)
            if not isinstance(digest, str) or len(digest) != 64 \
                    or any(c not in '0123456789abcdef' for c in digest):
                raise ValueError('bad digest')
        except ValueError as error:
            raise CorpusError(f'malformed stored sample: {error}') from error
        samples.append({
            'sample_id': sample_id,
            'lesson_id': lesson_id,
            'table_id': table_id,
            'participants': participants,
            'digest': digest,
        })

    objects = {}
    for digest, payload in connection.execute('SELECT digest, payload FROM objects'):
        try:
            settlement = json.loads(payload)
            if not isinstance(settlement, dict) or _digest(settlement) != digest:
                raise ValueError('settlement digest mismatch')
        except ValueError as error:
            raise CorpusError(f'malformed stored object: {error}') from error
        objects[digest] = settlement

    permissions = {}
    for player_id, allowed in connection.execute(
        'SELECT player_id, allowed FROM permissions'
    ):
        try:
            ident(player_id)
            if allowed not in (0, 1):
                raise ValueError('bad consent flag')
        except ValueError as error:
            raise CorpusError(f'malformed stored permission: {error}') from error
        permissions[player_id] = bool(allowed)

    if not {sample['digest'] for sample in samples} <= set(objects):
        raise CorpusError('malformed corpus: sample without settlement object')
    return {
        'public_path': meta['public_path'],
        'generation': generation,
        'package': package,
        'package_text': meta['package'],
        'samples': samples,
        'objects': objects,
        'permissions': permissions,
    }


def _build_package(generation, samples, objects, permissions):
    """Render the public package from the whole private corpus.

    Consent is judged per hand with the permissions in effect after the
    batch; the two-table threshold is judged per lesson over the distinct
    internal table ids of the consenting samples only.
    """
    by_lesson = {}
    for sample in samples:
        if all(permissions.get(player_id, False)
               for player_id in sample['participants']):
            by_lesson.setdefault(sample['lesson_id'], []).append(sample)
    lessons = {}
    visible_objects = {}
    for lesson_id in sorted(by_lesson):
        members = by_lesson[lesson_id]
        if len({sample['table_id'] for sample in members}) < MIN_TABLES_PER_LESSON:
            continue
        lessons[lesson_id] = [
            {'sample_id': sample['sample_id'], 'digest': sample['digest']}
            for sample in sorted(members, key=lambda sample: sample['sample_id'])
        ]
        for sample in members:
            visible_objects[sample['digest']] = objects[sample['digest']]
    return {'generation': generation, 'lessons': lessons,
            'objects': visible_objects}


def _check_batch(model, new_samples):
    existing = {sample['sample_id'] for sample in model['samples']}
    duplicates = sorted({sample['sample_id'] for sample in new_samples} & existing)
    if duplicates:
        raise CorpusError(
            f'sample_id already recorded: {", ".join(duplicates)}')
    if len(model['samples']) + len(new_samples) > MAX_SAMPLES:
        raise CorpusError(f'corpus holds at most {MAX_SAMPLES} samples')


def _record_batch(connection, new_samples, new_permissions, generation, package):
    connection.executemany(
        'INSERT INTO samples(sample_id, lesson_id, table_id, participants, digest)'
        ' VALUES (?, ?, ?, ?, ?)',
        [(sample['sample_id'], sample['lesson_id'], sample['table_id'],
          _canonical(sample['participants']), sample['digest'])
         for sample in new_samples],
    )
    connection.executemany(
        'INSERT OR IGNORE INTO objects(digest, payload) VALUES (?, ?)',
        [(sample['digest'], _canonical(sample['settlement']))
         for sample in new_samples],
    )
    connection.executemany(
        'INSERT OR REPLACE INTO permissions(player_id, allowed) VALUES (?, ?)',
        [(player_id, int(allowed)) for player_id, allowed in new_permissions],
    )
    connection.execute(
        "UPDATE meta SET value=? WHERE key='generation'", (str(generation),))
    connection.execute(
        "UPDATE meta SET value=? WHERE key='package'", (_canonical(package),))


def apply(state, source):
    """Install one batch as a whole and publish the next generation.

    Input problems reject the batch before anything is recorded.  The
    private commit (samples, permissions, rendered package) is the point
    of no return; the atomic rename then makes exactly that committed
    generation visible.  A storage error after the commit is reported as
    failure and later completed by ``recover`` — never reported as success.
    """
    new_samples, new_permissions = _validate_batch(source)
    connection = _open(state, True)
    try:
        connection.execute('BEGIN IMMEDIATE')
        try:
            model = _load_model(connection)
            _check_batch(model, new_samples)
            samples = model['samples'] + [
                {key: sample[key] for key in
                 ('sample_id', 'lesson_id', 'table_id', 'participants', 'digest')}
                for sample in new_samples
            ]
            objects = dict(model['objects'])
            for sample in new_samples:
                objects.setdefault(sample['digest'], sample['settlement'])
            permissions = dict(model['permissions'])
            permissions.update(new_permissions)
            generation = model['generation'] + 1
            package = _build_package(generation, samples, objects, permissions)
            payload = _canonical(package).encode('utf-8')

            public_path = Path(model['public_path'])
            public_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = _write_temp(public_path.parent, payload)
            try:
                _record_batch(connection, new_samples, new_permissions,
                              generation, package)
                connection.commit()
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
            try:
                _rename(temporary, public_path)
                _fsync_directory(public_path.parent)
            finally:
                temporary.unlink(missing_ok=True)
        except BaseException:
            connection.rollback()
            raise
    finally:
        connection.close()
    return {
        'generation': generation,
        'recorded': len(new_samples),
        'samples': len(samples),
        'visible_lessons': len(package['lessons']),
        'visible_samples': sum(len(entries) for entries in package['lessons'].values()),
    }


def _read_public_generation(path):
    """The generation a reader at the fixed path would see, or None."""
    try:
        with open(path, 'rb') as stream:
            data = stream.read(64_000_001)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise CorpusError(f'cannot read public file: {error}') from error
    if len(data) > 64_000_000:
        raise CorpusError('public file is not intact: too large')
    try:
        package = json.loads(data.decode('utf-8'))
    except (UnicodeError, ValueError) as error:
        raise CorpusError(f'public file is not intact: {error}') from error
    if not isinstance(package, dict) or type(package.get('generation')) is not int:
        raise CorpusError('public file is not a corpus package')
    return package['generation']


def recover(state):
    """Bring the fixed public path in line with the committed private state.

    Runs in a new process and never re-reads any import file.  A committed
    batch whose generation never became visible is completed by publishing
    the stored package; a generation that already became visible is kept
    as is — recovery never adds a generation and never rolls one back.
    """
    connection = _open(state, True)
    try:
        model = _load_model(connection)
    finally:
        connection.close()
    generation = model['generation']
    public_path = Path(model['public_path'])
    action = 'none'
    visible = _read_public_generation(public_path)
    if visible is None or visible < generation:
        _publish(public_path, model['package_text'].encode('utf-8'))
        action = 'published'
    elif visible > generation:
        raise CorpusError(
            f'public file generation {visible} is newer than corpus '
            f'generation {generation}')
    keep = {os.path.abspath(public_path), os.path.abspath(state)}
    _remove_stray_temps(public_path.parent, keep)
    return {'generation': generation, 'action': action}
