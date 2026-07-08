"""Entry point: python -m jnxfeed <subcommand>.

Subcommands land with later tasks (T3.3 probe/capture, T7.1 views); this
stub only reports what exists so `python -m jnxfeed` always works.
"""
import sys


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    sys.stderr.write(
        "jnxfeed: no subcommands implemented yet "
        "(coming: probe, capture, replay, static, tail, book, stats)\n"
    )
    return 0 if not argv else 2


if __name__ == "__main__":
    sys.exit(main())
