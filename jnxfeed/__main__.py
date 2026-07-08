"""Entry point: python -m jnxfeed <subcommand>.

Implemented subcommands (T3.3 connectivity kit):
  probe    — connect + SoupBinTCP login diagnostic (ITCH or GLIMPSE)
  capture  — stream a live ITCH session to a .itch file

Later tasks add: replay, static, tail, book, stats (T7.1). Unknown or
not-yet-implemented subcommands print a pointer and exit nonzero, so
`python -m jnxfeed` always works.
"""
import sys

_IMPLEMENTED = ("probe", "capture")
_PLANNED = ("replay", "static", "tail", "book", "stats")

_USAGE = """\
usage: python -m jnxfeed <subcommand> [options]

subcommands:
  probe    connect + SoupBinTCP login diagnostic (--help for options)
  capture  stream a live ITCH session to a .itch file (--help for options)

not yet implemented (coming with later tasks): {planned}
""".format(planned=", ".join(_PLANNED))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        sys.stderr.write(_USAGE)
        return 0 if argv else 2

    cmd = argv[0]
    rest = argv[1:]
    if cmd == "probe":
        from jnxfeed.cli import probe
        return probe.main(rest)
    if cmd == "capture":
        from jnxfeed.cli import capture
        return capture.main(rest)
    if cmd in _PLANNED:
        sys.stderr.write(
            "jnxfeed: subcommand {!r} is not implemented yet "
            "(coming with a later task)\n".format(cmd)
        )
        return 2
    sys.stderr.write("jnxfeed: unknown subcommand {!r}\n\n{}".format(cmd, _USAGE))
    return 2


if __name__ == "__main__":
    sys.exit(main())
