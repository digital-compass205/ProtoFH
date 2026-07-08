# Japannext connectivity — checklist, runbook, open questions

The connectivity kit (`jnxfeed probe`, `jnxfeed capture`) is built and
tested against the published specs and the official sample captures, but
has never seen a live Japannext endpoint. This file is what we need to
ask for, what to run on day one of UAT access, and what is still unknown.

Japannext technical support: **ito@japannext.co.jp**.
Their published *Client Connectivity Testing Schedule* (see
www.japannext.co.jp/en/support) lists UAT slots.

## 1. What to request from Japannext before UAT

Service configuration (none of this is in the public PDFs):

- [ ] **ITCH-over-SoupBinTCP endpoints** (host + TCP port) for the
      equities feed — and whether DAY / NGHT / DAYX / DAYU are separate
      endpoints/sessions or one combined feed.
- [ ] **GLIMPSE endpoints** (host + TCP port) matching each ITCH feed.
- [ ] **Credentials**: SoupBinTCP username(s) + password(s). Note the
      spec rule: each username is bound to a specific TCP port —
      username/port pairs cannot be mixed (login reject code `A`).
      Confirm which username goes with which port.
- [ ] **Replay policy**: is logging in with requested sequence 1 (full
      session replay) permitted at any time of day? Any rate limits or
      per-day connection limits we should respect?
- [ ] **UAT schedule**: which dates/hours is the UAT feed live, and does
      it carry realistic data volumes?
- [ ] **SFTP access** for daily files — especially the **ITCH Binary
      Data File** (full-day raw feed, same `.itch` framing this repo
      uses natively) for offline replay/regression testing.

## 2. Open questions (tracked from JNX_PLAN.md §6)

1. Are DAY/NGHT/DAYX/DAYU served as separate SoupBinTCP sessions or one
   combined feed? Can order numbers collide across groups within one
   feed? (`E`/`D`/`U` messages carry no group field; the spec says order
   numbers are unique per day *per order book group*.)
2. Full service configuration — hosts/ports/credential assignments (see
   checklist above).
3. GLIMPSE login: the spec mandates a **blank Requested Session**; what
   should Requested Sequence Number be? (We use 1; please confirm.)
4. Is full-session replay (login at seq 1) permitted at any time of day?
   Rate limits?
5. Can we get SFTP access for the daily ITCH Binary Data File?

## 3. UAT day runbook

Run these in order; every command prints a human-readable report and
`--report FILE` also writes JSON you can attach to a support e-mail.

```sh
# 1. ITCH endpoint: connect + login + look at the first messages.
python -m jnxfeed probe --host <ITCH_HOST> --port <ITCH_PORT> \
    --user <USER> --pass <PASS> --seq 0 --messages 20 \
    --report probe_itch.json
# --seq 0 = "most recent" — cheap smoke test that doesn't replay the day.

# 2. GLIMPSE endpoint: pull a full snapshot, confirm the G handoff seq.
python -m jnxfeed probe --host <GLIMPSE_HOST> --port <GLIMPSE_PORT> \
    --user <USER> --pass <PASS> --glimpse --timeout 60 \
    --report probe_glimpse.json

# 3. Full-session capture -> real sample data for the test suite.
python -m jnxfeed capture --host <ITCH_HOST> --port <ITCH_PORT> \
    --user <USER> --pass <PASS> --seq 1 --out uat_day1.itch
# Runs until server end-of-session (Z) or Ctrl-C; reconnects and resumes
# by itself; writes uat_day1.itch + uat_day1.itch.meta.json.
```

Expected outcomes and what to do otherwise:

| Result | Meaning / action |
|---|---|
| `probe` exit 0, `login: ACCEPTED session=... next_seq=...` | All good. Note the session id and sequence — include them in any support mail. |
| exit 3, `connect: FAILED` | Network path problem (routing/firewall) — check with your network team first, then Japannext. |
| exit 4, reject code `A` | Bad username/password **or wrong username↔port pairing**. Re-check which username belongs on which port before mailing support. |
| exit 4, reject code `S` | Requested session unavailable — we requested a stale/nonexistent session. Retry with a blank session (default). |
| exit 5 | Connected and logged in but the protocol didn't behave as expected — send the JSON report to Japannext support and to us. |
| `probe --glimpse` ends `snapshot did not complete (no G)` | Increase `--timeout` (snapshots are thousands of messages); if it persists, capture the JSON report for support. |

After a successful capture, validate the recording offline:

```sh
python -m jnxfeed.cli.fixtures --help   # fixture/manifest tooling
python - <<'EOF'
from jnxfeed import itchfile
from jnxfeed.itch import codec
n = 0
for m in itchfile.read_file("uat_day1.itch"):
    codec.decode(m); n += 1
print(n, "messages decode cleanly")
EOF
```

## 4. Defaults used by this repo

Sample-derived values (from the official captures — simulator/test
defaults only, NOT production configuration): ITCH TCP port 15001,
GLIMPSE TCP port 15002. Heartbeats: send after 1 s idle, declare the
peer dead after 15 s silence (spec values; `--idle-timeout` overrides).
