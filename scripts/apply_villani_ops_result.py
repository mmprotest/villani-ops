#!/usr/bin/env python3
from __future__ import annotations
import argparse
import sys
from pathlib import Path


def materialize_cmd(args: argparse.Namespace) -> int:
    try:
        from villani_ops.materialize import materialize_latest
    except Exception as exc:
        print(f"[materialize] failed to import canonical materializer: {exc}", file=sys.stderr)
        return 1
    result = materialize_latest(Path(args.workspace), Path(args.repo), policy=args.policy)
    if result.status == 'failed':
        if result.error:
            print(f"[materialize] error: {result.error}", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Apply Villani Ops result artifacts to a repository.')
    sub = parser.add_subparsers(dest='command')
    mat = sub.add_parser('materialize')
    mat.add_argument('--workspace', required=True)
    mat.add_argument('--repo', required=True)
    mat.add_argument('--policy', default='accepted')
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == 'materialize':
        return materialize_cmd(args)
    parser.print_help()
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
