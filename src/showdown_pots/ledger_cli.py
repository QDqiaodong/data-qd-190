import argparse
import json
import sqlite3
import sys

from . import ledger
from .io import load


def _print(result):
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))


def main(argv=None):
    parser = argparse.ArgumentParser(prog='showdown-ledger')
    parser.add_argument('--state', required=True)
    commands = parser.add_subparsers(dest='command', required=True)

    command = commands.add_parser('init')
    command.add_argument('--source', required=True)

    command = commands.add_parser('apply')
    command.add_argument('--source', required=True)

    commands.add_parser('preview')

    command = commands.add_parser('release')
    command.add_argument('--request-id', required=True)
    command.add_argument('--through', required=True, type=int)

    command = commands.add_parser('read-release')
    command.add_argument('--request-id', required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == 'init':
            result = ledger.init(args.state, load(args.source))
        elif args.command == 'apply':
            result = ledger.apply(args.state, load(args.source))
        elif args.command == 'preview':
            result = ledger.preview(args.state)
        elif args.command == 'release':
            result = ledger.release(args.state, args.request_id, args.through)
        else:
            result = ledger.read_release(args.state, args.request_id)
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
    _print(result)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
