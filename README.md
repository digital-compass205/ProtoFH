# jnxfeed — Japannext PTS ITCH feed handler (prototype)

A prototype market data feed handler for the Japannext PTS equities ITCH
feed over SoupBinTCP, built to evaluate AI-agent-assisted development.
The full design, protocol cheat sheet, and task breakdown live in
`/workspace/JNX_PLAN.md`; official specs and sample captures are in
`/workspace/jnx-specs/`.

## Target platform

- **Python 3.6.4** (RHEL 8.10), **stdlib-only at runtime**.
- Development may happen on newer interpreters, but code must stay within
  the 3.6 subset defined in JNX_PLAN.md §0. A static guard
  (`tests/unit/test_py36_compat.py`) rejects the most common 3.7+ idioms;
  the authoritative check is running the suite on real 3.6 (below).

## Running the tests

Two paths:

```sh
# Fast local run — uses python3.6 if installed, else your python3:
make test

# Authoritative run — Python 3.6 inside the RHEL 8 (UBI8) container,
# matching the deployment target (requires docker):
make test-docker
```

`pytest==7.0.1` is pinned in `Dockerfile.dev` — it is the last pytest
release that supports Python 3.6.

## Layout

See JNX_PLAN.md §2. Summary: `jnxfeed/itch` (message codec), `jnxfeed/soup`
(SoupBinTCP), `jnxfeed/net` (selectors reactor), `jnxfeed/book` (refdata,
order books, trade tape), `jnxfeed/sim` (exchange simulator),
`jnxfeed/cli` (probe/capture/replay/views), `tests/` (unit, integration,
fixtures extracted from the official sample captures).

## Usage

`python -m jnxfeed` — subcommands arrive with later tasks
(probe, capture, replay, static, tail, book, stats).
