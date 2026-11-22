"""CLI for the teaching-sample corpus: ``showdown-corpus --state PATH ...``."""
import argparse
import json
import sqlite3
import sys

from . import corpus
from .io import load


def main(argv=None):
    parser = argparse.ArgumentParser(prog='showdown-corpus')
    parser.add_argument('--state', required=True)
    commands = parser.add_subparsers(dest='command', required=True)

    command = commands.add_parser('init')
    command.add_argument('--public', required=True)

    command = commands.add_parser('apply')
    command.add_argument('--source', required=True)

    commands.add_parser('recover')

    args = parser.parse_args(argv)
    try:
        if args.command == 'init':
            result = corpus.init(args.state, args.public)
        elif args.command == 'apply':
            result = corpus.apply(args.state, load(args.source))
        else:
            result = corpus.recover(args.state)
    except (
        OSError,
        ValueError,
        UnicodeError,
        RecursionError,
        sqlite3.Error,
        KeyError,
        TypeError,
    ) as error:
        print(f'error: {error}', file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
