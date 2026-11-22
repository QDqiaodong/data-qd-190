"""CLI for replaying recorded teaching-table betting: ``showdown-replay``."""
import argparse
import json
import sys

from .io import load, publish
from .replay import replay


def main(argv=None):
    parser = argparse.ArgumentParser(prog='showdown-replay')
    parser.add_argument('--source', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    try:
        result = replay(load(args.source))
        publish(args.output, result)
    except (OSError, ValueError, UnicodeError, RecursionError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 2
    if 'branches' in result:
        summary = {
            'branches': len(result['branches']),
            'ok': sum(item['status'] == 'ok' for item in result['branches']),
        }
    else:
        summary = {
            'street': result['next']['street'] if result['next'] else None,
            'settled': result['settlement'] is not None,
        }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
