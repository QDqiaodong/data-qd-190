"""CLI for two-source reconciliation: ``showdown-reconcile --source --output``."""
import argparse
import json
import sys

from .io import load, publish
from .reconcile import reconcile


def main(argv=None):
    parser = argparse.ArgumentParser(prog='showdown-reconcile')
    parser.add_argument('--source', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    try:
        result = reconcile(load(args.source))
        publish(args.output, result)
    except (OSError, ValueError, UnicodeError, RecursionError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 2
    print(json.dumps(
        {
            'status': result['status'],
            'max_pairs': result['max_pairs'],
            'reconciled': len(result['reconciled']),
        },
        ensure_ascii=False,
        sort_keys=True,
    ))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
