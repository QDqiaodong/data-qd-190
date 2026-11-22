"""CLI for the occluded-hand audit: ``showdown-audit --source --output``."""
import argparse
import json
import sys

from .audit import audit
from .io import load, publish


def main(argv=None):
    parser = argparse.ArgumentParser(prog='showdown-audit')
    parser.add_argument('--source', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    try:
        result = audit(load(args.source))
        publish(args.output, result)
    except (OSError, ValueError, UnicodeError, RecursionError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 2
    print(json.dumps(
        {'status': result['status'], 'pots': len(result['pots'])},
        ensure_ascii=False, sort_keys=True,
    ))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
