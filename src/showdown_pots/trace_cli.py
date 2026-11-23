"""CLI for marked-chip batch tracing: ``showdown-trace --source --output``."""
import argparse
import json
import sys

from .io import load, publish
from .trace import trace


def main(argv=None):
    parser = argparse.ArgumentParser(prog='showdown-trace')
    parser.add_argument('--source', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    try:
        result = trace(load(args.source))
        publish(args.output, result)
    except (OSError, ValueError, UnicodeError, RecursionError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 2
    print(json.dumps(
        {
            'players': len(result['balances']),
            'groups': len(result['groups']),
            'counted_possible': result['counted_possible'],
        },
        ensure_ascii=False, sort_keys=True,
    ))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
