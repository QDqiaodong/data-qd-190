"""Regression tests for the revocable teaching-sample corpus.

The acceptance hand is the tied board ``Tc Jc Qc Kc Ac`` with button 0:
seats 0 and 1 live (``2d 3d`` / ``4d 5d``), seat 2 folded, everyone
commits 1.  The only pot holds 3 chips; winners in odd-chip order are
seats 1, 0 (awards 2, 1); per-seat nets are 0, +1, -1.  Samples that only
rename the participants or reorder the input players array settle to the
same object and digest.

Crash tests inject faults at the two I/O boundaries of the installer
(``_write_temp`` before the private commit, ``_rename`` which makes a
generation visible) so the before/after-visibility cases are stable;
recovery itself is then exercised cross-process through the real CLI.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(ROOT / 'src'))

from showdown_pots import corpus
from showdown_pots.corpus import CorpusError
from showdown_pots.service import process

BOARD = ['Tc', 'Jc', 'Qc', 'Kc', 'Ac']


def tie_hand(folder, seat0, seat1, order=(0, 1, 2)):
    """The acceptance tie hand; ``folder`` sits at seat 2, all commit 1."""
    players = [
        {'id': seat0, 'seat': 0, 'committed': 1, 'folded': False,
         'hole': ['2d', '3d']},
        {'id': seat1, 'seat': 1, 'committed': 1, 'folded': False,
         'hole': ['4d', '5d']},
        {'id': folder, 'seat': 2, 'committed': 1, 'folded': True, 'hole': []},
    ]
    return {'board': list(BOARD), 'button_seat': 0,
            'players': [players[index] for index in order]}


EXPECTED_OBJECT = {
    'button_seat': 0,
    'players': [
        {'seat': 0, 'folded': False, 'committed': 1, 'net': 0},
        {'seat': 1, 'folded': False, 'committed': 1, 'net': 1},
        {'seat': 2, 'folded': True, 'committed': 1, 'net': -1},
    ],
    'pots': [{'pot': 1, 'amount': 3, 'eligible_seats': [0, 1],
              'winner_seats_in_odd_chip_order': [1, 0],
              'awards': {'0': 1, '1': 2}}],
    'uncalled_returns': [],
}

EXPECTED_CANONICAL = (
    '{"button_seat":0,"players":['
    '{"committed":1,"folded":false,"net":0,"seat":0},'
    '{"committed":1,"folded":false,"net":1,"seat":1},'
    '{"committed":1,"folded":true,"net":-1,"seat":2}],'
    '"pots":[{"amount":3,"awards":{"0":1,"1":2},"eligible_seats":[0,1],'
    '"pot":1,"winner_seats_in_odd_chip_order":[1,0]}],'
    '"uncalled_returns":[]}'
)
EXPECTED_DIGEST = hashlib.sha256(EXPECTED_CANONICAL.encode('utf-8')).hexdigest()


class _Killed(Exception):
    """Simulates the maintenance process dying at an I/O boundary."""


def _kill_before_rename(source, target):
    raise _Killed('killed before the new generation became visible')


def _kill_after_rename(source, target):
    os.replace(source, target)
    raise _Killed('killed after the new generation became visible')


def _kill_before_commit(directory, payload):
    raise _Killed('killed before the batch was committed')


class CorpusCli:
    def __init__(self, test, state):
        self.test = test
        self.state = Path(state)
        self.sources = []

    def run(self, *arguments, expect=0):
        env = dict(os.environ)
        env['PYTHONPATH'] = str(ROOT / 'src')
        completed = subprocess.run(
            [sys.executable, '-m', 'showdown_pots.corpus_cli',
             '--state', str(self.state), *arguments],
            cwd=str(ROOT), env=env, capture_output=True, text=True,
        )
        self.test.assertEqual(
            expect, completed.returncode,
            msg=f'argv={arguments} stderr={completed.stderr!r} '
                f'stdout={completed.stdout!r}',
        )
        if expect != 0:
            self.test.assertEqual('', completed.stdout)
            self.test.assertIn('error:', completed.stderr)
            return None
        return json.loads(completed.stdout)

    def write(self, value):
        descriptor, name = tempfile.mkstemp(
            dir=str(self.state.parent), suffix='.json')
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(value, stream)
        self.sources.append(Path(name))
        return Path(name)

    def init(self, public, expect=0):
        return self.run('init', '--public', str(public), expect=expect)

    def apply(self, payload, expect=0):
        return self.run('apply', '--source', str(self.write(payload)),
                        expect=expect)

    def recover(self, expect=0):
        return self.run('recover', expect=expect)


class CorpusTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name) / 'corpus.db'
        self.public = Path(self.directory.name) / 'dist' / 'package.json'
        self.cli = CorpusCli(self, self.state)

    def package(self):
        return json.loads(self.public.read_text(encoding='utf-8'))

    @staticmethod
    def sample(sample_id, lesson, table, hand):
        return {'sample_id': sample_id, 'lesson_id': lesson,
                'table_id': table, 'hand': hand}

    @staticmethod
    def grant(*player_ids):
        return [{'player_id': player_id, 'allowed': True}
                for player_id in player_ids]

    @staticmethod
    def revoke(*player_ids):
        return [{'player_id': player_id, 'allowed': False}
                for player_id in player_ids]

    @staticmethod
    def entry(sample_id):
        return {'sample_id': sample_id, 'digest': EXPECTED_DIGEST}

    def setup_five_samples(self):
        """Acceptance batches 1-2: five samples, one shared object, gen 2."""
        self.cli.init(self.public)
        first = self.cli.apply({
            'samples': [
                self.sample('x1', 'L', 'T1', tie_hand('A', 'B', 'C')),
                self.sample('x1b', 'L', 'T1',
                            tie_hand('A', 'B', 'C', order=(2, 0, 1))),
            ],
            'permissions': self.grant('A', 'B', 'C'),
        })
        self.assertEqual(first, {'generation': 1, 'recorded': 2, 'samples': 2,
                                 'visible_lessons': 0, 'visible_samples': 0})
        second = self.cli.apply({
            'samples': [
                self.sample('x2', 'L', 'T2', tie_hand('G2', 'G0', 'G1')),
                self.sample('y1', 'M', 'T3', tie_hand('E', 'D', 'F')),
                self.sample('y2', 'M', 'T4', tie_hand('H2', 'H0', 'H1')),
            ],
            'permissions': self.grant('G0', 'G1', 'G2', 'D', 'E', 'F',
                                      'H0', 'H1', 'H2'),
        })
        self.assertEqual(second, {'generation': 2, 'recorded': 3, 'samples': 5,
                                  'visible_lessons': 2, 'visible_samples': 5})

    # -- projection and digest -------------------------------------------

    def test_projection_digest_and_canonical_form(self):
        self.assertEqual(corpus._canonical(EXPECTED_OBJECT), EXPECTED_CANONICAL)
        self.assertEqual(corpus._digest(EXPECTED_OBJECT), EXPECTED_DIGEST)
        # Renaming players or reordering the input array keeps the digest.
        for hand in (
            tie_hand('A', 'B', 'C'),
            tie_hand('Someone', 'Else', 'Entirely'),
            tie_hand('A', 'B', 'C', order=(2, 0, 1)),
            tie_hand('A', 'B', 'C', order=(1, 2, 0)),
        ):
            settlement = corpus._project(hand, process(hand))
            self.assertEqual(settlement, EXPECTED_OBJECT)
            self.assertEqual(corpus._digest(settlement), EXPECTED_DIGEST)

    # -- acceptance flow: threshold, withdrawal, republication -----------

    def test_lesson_threshold_withdrawal_and_republish(self):
        self.cli.init(self.public)
        self.assertEqual(self.package(),
                         {'generation': 0, 'lessons': {}, 'objects': {}})

        # Two hands from one table only: everyone consents, still invisible.
        self.cli.apply({
            'samples': [
                self.sample('x1', 'L', 'T1', tie_hand('A', 'B', 'C')),
                self.sample('x1b', 'L', 'T1',
                            tie_hand('A', 'B', 'C', order=(2, 0, 1))),
            ],
            'permissions': self.grant('A', 'B', 'C'),
        })
        self.assertEqual(self.package(),
                         {'generation': 1, 'lessons': {}, 'objects': {}})

        # A second table for L and two tables for M: five references, one object.
        self.cli.apply({
            'samples': [
                self.sample('x2', 'L', 'T2', tie_hand('G2', 'G0', 'G1')),
                self.sample('y1', 'M', 'T3', tie_hand('E', 'D', 'F')),
                self.sample('y2', 'M', 'T4', tie_hand('H2', 'H0', 'H1')),
            ],
            'permissions': self.grant('G0', 'G1', 'G2', 'D', 'E', 'F',
                                      'H0', 'H1', 'H2'),
        })
        package = self.package()
        self.assertEqual(package['generation'], 2)
        self.assertEqual(package['lessons'], {
            'L': [self.entry('x1'), self.entry('x1b'), self.entry('x2')],
            'M': [self.entry('y1'), self.entry('y2')],
        })
        self.assertEqual(package['objects'], {EXPECTED_DIGEST: EXPECTED_OBJECT})

        # Withdrawing only A: x1/x1b drop out, L falls under the threshold,
        # M keeps the shared object alive.
        self.cli.apply({'samples': [], 'permissions': self.revoke('A')})
        package = self.package()
        self.assertEqual(package['generation'], 3)
        self.assertEqual(package['lessons'],
                         {'M': [self.entry('y1'), self.entry('y2')]})
        self.assertEqual(package['objects'], {EXPECTED_DIGEST: EXPECTED_OBJECT})

        # Withdrawing only E: M falls under the threshold; both maps empty.
        self.cli.apply({'samples': [], 'permissions': self.revoke('E')})
        self.assertEqual(self.package(),
                         {'generation': 4, 'lessons': {}, 'objects': {}})

        # A fresh table republishes x2 and the new x3; x1/x1b stay withdrawn.
        self.cli.apply({
            'samples': [self.sample('x3', 'L', 'T5', tie_hand('R2', 'R0', 'R1'))],
            'permissions': self.grant('R0', 'R1', 'R2'),
        })
        package = self.package()
        self.assertEqual(package['generation'], 5)
        self.assertEqual(package['lessons'],
                         {'L': [self.entry('x2'), self.entry('x3')]})
        self.assertEqual(package['objects'], {EXPECTED_DIGEST: EXPECTED_OBJECT})

    def test_permission_is_per_hand_but_threshold_counts_tables(self):
        self.cli.init(self.public)
        self.cli.apply({
            'samples': [
                self.sample('k1', 'K', 'T6', tie_hand('P2', 'P0', 'P1')),
                self.sample('k2', 'K', 'T6', tie_hand('Q2', 'Q0', 'Q1')),
                self.sample('k3', 'K', 'T7', tie_hand('R2', 'R0', 'R1')),
            ],
            'permissions': self.grant('P0', 'P1', 'P2', 'Q0', 'Q1', 'Q2',
                                      'R0', 'R1', 'R2'),
        })
        self.assertEqual(
            [entry['sample_id'] for entry in self.package()['lessons']['K']],
            ['k1', 'k2', 'k3'])
        # Revoking one participant of k1 hides only that hand; table T6
        # still counts through k2, so the lesson and k3 survive.  k1 and k2
        # share one settlement object, yet consent is judged per hand: the
        # shared object stays because k2 still references it.
        self.cli.apply({'samples': [], 'permissions': self.revoke('P0')})
        package = self.package()
        self.assertEqual(
            [entry['sample_id'] for entry in package['lessons']['K']],
            ['k2', 'k3'])
        self.assertEqual(list(package['objects']), [EXPECTED_DIGEST])

    def test_zero_commit_participant_must_consent(self):
        hand = tie_hand('Z2', 'Z0', 'Z1')
        hand['players'].append({'id': 'Z3', 'seat': 3, 'committed': 0,
                                'folded': False, 'hole': ['6d', '7d']})
        self.cli.init(self.public)
        self.cli.apply({
            'samples': [
                self.sample('z1', 'Z', 'T8', hand),
                self.sample('z2', 'Z', 'T9', tie_hand('W2', 'W0', 'W1')),
            ],
            'permissions': self.grant('Z0', 'Z1', 'Z2', 'W0', 'W1', 'W2'),
        })
        # No record for Z3 means no consent: z1 is ineligible and the
        # lesson has only one table left, so nothing is visible.
        self.assertEqual(self.package()['lessons'], {})
        self.cli.apply({'samples': [], 'permissions': self.grant('Z3')})
        self.assertEqual(
            [entry['sample_id'] for entry in self.package()['lessons']['Z']],
            ['z1', 'z2'])

    # -- repository fixture projection ------------------------------------

    def test_repository_fixture_projection(self):
        hand = json.loads((ROOT / 'fixtures' / 'input.json')
                          .read_text(encoding='utf-8'))
        self.cli.init(self.public)
        self.cli.apply({
            'samples': [
                self.sample('f1', 'F', 'U1', hand),
                self.sample('f2', 'F', 'U2', tie_hand('N2', 'N0', 'N1')),
            ],
            'permissions': self.grant('ace', 'king', 'straight', 'fold',
                                      'N0', 'N1', 'N2'),
        })
        package = self.package()
        digest = next(entry['digest'] for entry in package['lessons']['F']
                      if entry['sample_id'] == 'f1')
        obj = package['objects'][digest]
        self.assertEqual(obj['button_seat'], 0)
        self.assertEqual([pot['amount'] for pot in obj['pots']], [80, 90, 40])
        self.assertEqual([pot['eligible_seats'] for pot in obj['pots']],
                         [[0, 1, 2], [0, 1], [0]])
        self.assertEqual([pot['pot'] for pot in obj['pots']], [1, 2, 3])
        # The uncalled return is not another pot and keeps its v1 layer.
        self.assertEqual(obj['uncalled_returns'], [
            {'seat': 0, 'from_exclusive': 70, 'to_inclusive': 100,
             'amount': 30}])
        self.assertEqual([(player['seat'], player['net'])
                          for player in obj['players']],
                         [(0, 60), (1, -50), (2, 60), (3, -70)])
        self.assertEqual([sorted(player) for player in obj['players']],
                         [['committed', 'folded', 'net', 'seat']] * 4)
        self.assertEqual([sorted(pot) for pot in obj['pots']],
                         [['amount', 'awards', 'eligible_seats', 'pot',
                           'winner_seats_in_odd_chip_order']] * 3)
        self.assertEqual(obj['pots'][0]['awards'], {'2': 80})
        self.assertEqual(obj['pots'][1]['awards'], {'0': 90})
        self.assertEqual(obj['pots'][2]['awards'], {'0': 40})

    # -- atomicity ---------------------------------------------------------

    def test_unsettleable_hand_blocks_whole_batch(self):
        self.setup_five_samples()
        before = self.public.read_bytes()
        bad = tie_hand('V2', 'V0', 'V1')
        bad['players'][0]['hole'] = ['Tc', '3d']  # duplicates a board card
        self.cli.apply({
            'samples': [self.sample('bad1', 'M', 'T9', bad)],
            'permissions': self.revoke('H0'),
        }, expect=2)
        # Public file and private facts are both untouched.
        self.assertEqual(self.public.read_bytes(), before)
        self.assertEqual(self.cli.recover(), {'generation': 2, 'action': 'none'})
        self.assertEqual(
            [entry['sample_id'] for entry in self.package()['lessons']['M']],
            ['y1', 'y2'])
        # The failed sample id was never recorded and can be used again.
        result = self.cli.apply({
            'samples': [self.sample('bad1', 'M', 'T9',
                                    tie_hand('V2', 'V0', 'V1'))],
            'permissions': self.grant('V0', 'V1', 'V2'),
        })
        self.assertEqual(result['generation'], 3)
        self.assertEqual(
            [entry['sample_id'] for entry in self.package()['lessons']['M']],
            ['bad1', 'y1', 'y2'])

    def test_apply_batch_validation(self):
        self.cli.init(self.public)
        missing = CorpusCli(self, Path(self.directory.name) / 'missing.db')
        missing.apply({'samples': [], 'permissions': self.grant('A')}, expect=2)
        self.cli.apply({'samples': [], 'permissions': []}, expect=2)
        self.cli.apply({'samples': []}, expect=2)
        self.cli.apply({'permissions': []}, expect=2)
        self.cli.apply({'samples': [], 'permissions': [], 'extra': 1}, expect=2)
        self.cli.apply({'samples': [], 'permissions':
                        self.grant('A') + self.revoke('A')}, expect=2)
        self.cli.apply({'samples': [], 'permissions':
                        [{'player_id': 'A', 'allowed': 1}]}, expect=2)
        self.cli.apply({'samples': [], 'permissions':
                        [{'player_id': 'bad id', 'allowed': True}]}, expect=2)
        self.cli.apply({'samples': [self.sample('bad id', 'L', 'T1',
                                                tie_hand('A', 'B', 'C'))],
                        'permissions': []}, expect=2)
        self.cli.apply({'samples': [
            self.sample('s', 'L', 'T1', tie_hand('A', 'B', 'C')),
            self.sample('s', 'L', 'T2', tie_hand('D', 'E', 'F')),
        ], 'permissions': []}, expect=2)
        self.cli.apply({'samples': [
            self.sample(f's{i}', 'L', f'T{i}', tie_hand(f'F{i}', f'G{i}', f'H{i}'))
            for i in range(101)
        ], 'permissions': []}, expect=2)
        self.cli.apply({'samples': [], 'permissions':
                        self.grant(*[f'P{i}' for i in range(1001)])}, expect=2)
        # Nothing above installed a generation.
        self.assertEqual(self.package(),
                         {'generation': 0, 'lessons': {}, 'objects': {}})
        self.assertEqual(self.cli.recover(), {'generation': 0, 'action': 'none'})

    def test_sample_id_never_reusable(self):
        self.cli.init(self.public)
        self.cli.apply({'samples': [self.sample('s1', 'L', 'T1',
                                                tie_hand('A', 'B', 'C'))],
                        'permissions': []})
        # Even a byte-identical re-import conflicts, and a mixed batch
        # records nothing at all.
        self.cli.apply({'samples': [self.sample('s1', 'L', 'T1',
                                                tie_hand('A', 'B', 'C'))],
                        'permissions': []}, expect=2)
        self.cli.apply({'samples': [
            self.sample('s1', 'L', 'T1', tie_hand('A', 'B', 'C')),
            self.sample('s2', 'L', 'T2', tie_hand('D', 'E', 'F')),
        ], 'permissions': []}, expect=2)
        self.assertEqual(self.cli.recover(), {'generation': 1, 'action': 'none'})
        self.cli.apply({
            'samples': [self.sample('s2', 'L', 'T2', tie_hand('D', 'E', 'F'))],
            'permissions': self.grant('A', 'B', 'C', 'D', 'E', 'F'),
        })
        self.assertEqual(
            [entry['sample_id'] for entry in self.package()['lessons']['L']],
            ['s1', 's2'])

    def test_sample_cap(self):
        self.cli.init(self.public)
        for block in range(20):
            corpus.apply(str(self.state), {
                'samples': [
                    self.sample(f's{block}_{i}', 'L', f'T{block}_{i}',
                                tie_hand(f'A{block}_{i}', f'B{block}_{i}',
                                         f'C{block}_{i}'))
                    for i in range(100)
                ],
                'permissions': [],
            })
        self.assertEqual(self.cli.recover()['generation'], 20)
        self.cli.apply({'samples': [self.sample('overflow', 'L', 'Tx',
                                                tie_hand('A', 'B', 'C'))],
                        'permissions': []}, expect=2)
        self.assertEqual(self.cli.recover(), {'generation': 20, 'action': 'none'})

    # -- init guards -------------------------------------------------------

    def test_init_guards_and_empty_package(self):
        result = self.cli.init(self.public)
        self.assertEqual(result, {'generation': 0, 'public': str(self.public)})
        self.assertEqual(self.public.read_text(encoding='utf-8'),
                         '{"generation":0,"lessons":{},"objects":{}}')
        state_before = hashlib.sha256(self.state.read_bytes()).hexdigest()
        public_before = self.public.read_bytes()
        self.cli.init(self.public, expect=2)  # existing state is never taken over
        self.assertEqual(hashlib.sha256(self.state.read_bytes()).hexdigest(),
                         state_before)
        self.assertEqual(self.public.read_bytes(), public_before)

        other_public = Path(self.directory.name) / 'taken.json'
        other_public.write_text('{}', encoding='utf-8')
        other = CorpusCli(self, Path(self.directory.name) / 'other.db')
        other.init(other_public, expect=2)  # existing public file is never taken over
        self.assertFalse(other.state.exists())
        self.assertEqual(other_public.read_text(encoding='utf-8'), '{}')

        same = Path(self.directory.name) / 'same.path'
        CorpusCli(self, same).init(same, expect=2)  # state and public must differ
        self.assertFalse(same.exists())

    def test_init_rolls_back_when_public_creation_fails(self):
        with mock.patch.object(corpus, '_create_public',
                               side_effect=CorpusError('public file already exists')):
            with self.assertRaises(CorpusError):
                corpus.init(str(self.state), str(self.public))
        self.assertFalse(self.state.exists())
        self.assertFalse(self.public.exists())

    def test_public_path_is_fixed_at_init_time(self):
        """A relative --public is pinned to an absolute path at init time."""
        workdir = Path(self.directory.name) / 'work'
        workdir.mkdir()
        env = dict(os.environ)
        env['PYTHONPATH'] = str(ROOT / 'src')

        def run(cwd, *arguments, expect=0):
            completed = subprocess.run(
                [sys.executable, '-m', 'showdown_pots.corpus_cli',
                 '--state', str(self.state), *arguments],
                cwd=str(cwd), env=env, capture_output=True, text=True)
            self.assertEqual(expect, completed.returncode,
                             msg=f'{arguments}: {completed.stderr!r}')
            return json.loads(completed.stdout) if expect == 0 else None

        result = run(workdir, 'init', '--public', 'relative/pkg.json')
        fixed = workdir / 'relative' / 'pkg.json'
        self.assertEqual(result['public'], str(fixed))
        self.assertEqual(json.loads(fixed.read_text(encoding='utf-8')),
                         {'generation': 0, 'lessons': {}, 'objects': {}})

        # A later maintenance run from another directory still finds it.
        source = self.cli.write({
            'samples': [self.sample('s1', 'L', 'T1', tie_hand('A', 'B', 'C'))],
            'permissions': self.grant('A', 'B', 'C')})
        run(ROOT, 'apply', '--source', str(source))
        self.assertEqual(json.loads(fixed.read_text(encoding='utf-8'))['generation'], 1)
        self.assertFalse((ROOT / 'relative').exists())

    # -- crash and recovery ------------------------------------------------

    def test_crash_after_visibility_recovers_without_new_generation(self):
        self.setup_five_samples()
        with mock.patch.object(corpus, '_rename', side_effect=_kill_after_rename):
            with self.assertRaises(_Killed):
                corpus.apply(str(self.state),
                             {'samples': [], 'permissions': self.revoke('A')})

        # The new generation is already readable from the fixed path alone:
        # lesson L is gone, only M remains.
        package = self.package()
        self.assertEqual(package['generation'], 3)
        self.assertEqual(package['lessons'],
                         {'M': [self.entry('y1'), self.entry('y2')]})

        # The operator never received the receipt and the import files are
        # gone; cross-process recovery keeps the visible generation as is.
        for source in self.cli.sources:
            source.unlink(missing_ok=True)
        self.assertEqual(self.cli.recover(), {'generation': 3, 'action': 'none'})
        self.assertEqual(self.package()['generation'], 3)
        self.assertEqual(self.package()['lessons'],
                         {'M': [self.entry('y1'), self.entry('y2')]})

        # Continuing with x3 republishes only x2/x3 for L; A's hands stay out.
        result = self.cli.apply({
            'samples': [self.sample('x3', 'L', 'T5', tie_hand('R2', 'R0', 'R1'))],
            'permissions': self.grant('R0', 'R1', 'R2'),
        })
        self.assertEqual(result['generation'], 4)
        self.assertEqual(self.package()['lessons'], {
            'L': [self.entry('x2'), self.entry('x3')],
            'M': [self.entry('y1'), self.entry('y2')],
        })

    def test_crash_before_visibility_is_completed_by_recover(self):
        self.setup_five_samples()
        before = self.public.read_bytes()
        with mock.patch.object(corpus, '_rename', side_effect=_kill_before_rename):
            with self.assertRaises(_Killed):
                corpus.apply(str(self.state),
                             {'samples': [], 'permissions': self.revoke('A')})

        # The fixed path still serves the previous complete generation.
        self.assertEqual(self.public.read_bytes(), before)
        # Recovery completes the committed batch without adding a generation.
        self.assertEqual(self.cli.recover(),
                         {'generation': 3, 'action': 'published'})
        package = self.package()
        self.assertEqual(package['generation'], 3)
        self.assertEqual(package['lessons'],
                         {'M': [self.entry('y1'), self.entry('y2')]})
        self.assertEqual(self.cli.recover(), {'generation': 3, 'action': 'none'})

        # A later batch builds on the recovered state; A's hands stay out.
        result = self.cli.apply({
            'samples': [self.sample('x3', 'L', 'T5', tie_hand('R2', 'R0', 'R1'))],
            'permissions': self.grant('R0', 'R1', 'R2'),
        })
        self.assertEqual(result['generation'], 4)
        self.assertEqual(self.package()['lessons'], {
            'L': [self.entry('x2'), self.entry('x3')],
            'M': [self.entry('y1'), self.entry('y2')],
        })

    def test_crash_before_commit_abandons_the_whole_batch(self):
        self.setup_five_samples()
        before = self.public.read_bytes()
        with mock.patch.object(corpus, '_write_temp', side_effect=_kill_before_commit):
            with self.assertRaises(_Killed):
                corpus.apply(str(self.state), {
                    'samples': [self.sample('x3', 'L', 'T5',
                                            tie_hand('R2', 'R0', 'R1'))],
                    'permissions': self.revoke('A'),
                })

        # No half-batch private facts: no sample, no consent change, no generation.
        self.assertEqual(self.public.read_bytes(), before)
        self.assertEqual(self.cli.recover(), {'generation': 2, 'action': 'none'})
        result = self.cli.apply({
            'samples': [self.sample('x3', 'L', 'T5', tie_hand('R2', 'R0', 'R1'))],
            'permissions': self.grant('R0', 'R1', 'R2'),
        })
        self.assertEqual(result['generation'], 3)
        self.assertEqual(
            [entry['sample_id'] for entry in self.package()['lessons']['L']],
            ['x1', 'x1b', 'x2', 'x3'])

    def test_apply_after_unrecovered_crash_publishes_one_new_generation(self):
        self.setup_five_samples()
        with mock.patch.object(corpus, '_rename', side_effect=_kill_before_rename):
            with self.assertRaises(_Killed):
                corpus.apply(str(self.state),
                             {'samples': [], 'permissions': self.revoke('A')})
        self.assertEqual(self.package()['generation'], 2)  # still the old file
        # Without recovering first, the next legal batch installs exactly one
        # new generation carrying both batches' effects.
        result = self.cli.apply({
            'samples': [self.sample('x3', 'L', 'T5', tie_hand('R2', 'R0', 'R1'))],
            'permissions': self.grant('R0', 'R1', 'R2'),
        })
        self.assertEqual(result['generation'], 4)
        package = self.package()
        self.assertEqual(package['generation'], 4)
        self.assertEqual(package['lessons'], {
            'L': [self.entry('x2'), self.entry('x3')],
            'M': [self.entry('y1'), self.entry('y2')],
        })
        self.assertEqual(self.cli.recover(), {'generation': 4, 'action': 'none'})

    def test_generation_boundary_with_unchanged_visible_content(self):
        self.setup_five_samples()
        before = self.package()

        # A consent update that changes no visible sample still installs a
        # generation; killing after visibility keeps exactly that boundary.
        with mock.patch.object(corpus, '_rename', side_effect=_kill_after_rename):
            with self.assertRaises(_Killed):
                corpus.apply(str(self.state),
                             {'samples': [], 'permissions': self.grant('ZZ')})
        package = self.package()
        self.assertEqual(package['generation'], 3)
        self.assertEqual(package['lessons'], before['lessons'])
        self.assertEqual(package['objects'], before['objects'])
        self.assertEqual(self.cli.recover(), {'generation': 3, 'action': 'none'})
        self.assertEqual(self.package()['generation'], 3)

        # The same kind of batch killed before visibility is completed, not
        # doubled: recovery publishes the pending generation once.
        with mock.patch.object(corpus, '_rename', side_effect=_kill_before_rename):
            with self.assertRaises(_Killed):
                corpus.apply(str(self.state),
                             {'samples': [], 'permissions': self.grant('ZY')})
        self.assertEqual(self.package()['generation'], 3)
        self.assertEqual(self.cli.recover(),
                         {'generation': 4, 'action': 'published'})
        package = self.package()
        self.assertEqual(package['generation'], 4)
        self.assertEqual(package['lessons'], before['lessons'])
        self.assertEqual(package['objects'], before['objects'])
        self.assertEqual(self.cli.recover(), {'generation': 4, 'action': 'none'})

    def test_recover_republishes_missing_public_file(self):
        self.setup_five_samples()
        self.public.unlink()
        self.assertEqual(self.cli.recover(),
                         {'generation': 2, 'action': 'published'})
        self.assertEqual(self.package()['generation'], 2)
        self.assertEqual(
            [entry['sample_id'] for entry in self.package()['lessons']['L']],
            ['x1', 'x1b', 'x2'])

    def test_recover_cleans_interrupted_publication_leftovers(self):
        self.setup_five_samples()
        stray = self.public.parent / '.corpus-public-leftover.tmp'
        stray.write_text('partial', encoding='utf-8')
        self.assertEqual(self.cli.recover(), {'generation': 2, 'action': 'none'})
        self.assertFalse(stray.exists())
        self.assertEqual(self.package()['generation'], 2)

    def test_recover_unknown_state_and_foreign_public_file(self):
        self.cli.recover(expect=2)
        self.cli.init(self.public)
        # A public file from the future is rejected, never adopted.
        self.public.write_text(
            json.dumps({'generation': 9, 'lessons': {}, 'objects': {}}),
            encoding='utf-8')
        self.cli.recover(expect=2)
        # A non-package at the fixed path is rejected, never silently replaced.
        self.public.write_text('not json', encoding='utf-8')
        self.cli.recover(expect=2)

    # -- privacy of the public package -------------------------------------

    def test_package_carries_no_private_facts(self):
        self.cli.init(self.public)
        self.cli.apply({
            'samples': [
                self.sample('s1', 'lessonAlpha', 'tableOne',
                            tie_hand('playerFold', 'playerA', 'playerB')),
                self.sample('s2', 'lessonAlpha', 'tableTwo',
                            tie_hand('playerMute', 'playerC', 'playerD')),
            ],
            'permissions': self.grant('playerFold', 'playerA', 'playerB',
                                      'playerMute', 'playerC', 'playerD'),
        })
        text = self.public.read_text(encoding='utf-8')
        for leaked in ('playerFold', 'playerA', 'playerB', 'playerMute',
                       'playerC', 'playerD', 'tableOne', 'tableTwo',
                       'hole', 'best_five', 'board', 'allowed', 'permission',
                       str(self.state), 'Tc', 'Jc', 'Qc', 'Kc', 'Ac'):
            self.assertNotIn(leaked, text)
        package = self.package()
        self.assertEqual(sorted(package), ['generation', 'lessons', 'objects'])
        self.assertEqual(sorted(package['lessons']['lessonAlpha'][0]),
                         ['digest', 'sample_id'])
        # A reader verifies every reference from this one file alone.
        for entries in package['lessons'].values():
            for entry in entries:
                self.assertIn(entry['digest'], package['objects'])
                self.assertEqual(
                    corpus._digest(package['objects'][entry['digest']]),
                    entry['digest'])


if __name__ == '__main__':
    unittest.main()
