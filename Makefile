# Prefer a real 3.6 interpreter when present (the deployment target);
# otherwise fall back to whatever python3 is available for a fast local run.
PY := $(shell command -v python3.6 2>/dev/null || command -v python3)

.PHONY: test test-docker bench dist

test:
	$(PY) -m pytest -q

# Replay-throughput benchmark over the full 222k-message official sample
# (regenerates tests/fixtures/sample_udp.itch if missing; needs
# /workspace/jnx-specs for that).
bench:
	$(PY) bench/bench_replay.py

# Authoritative run: Python 3.6 inside the RHEL 8 (UBI8) container.
test-docker:
	docker build -f Dockerfile.dev -t jnxfeed-test .
	docker run --rm jnxfeed-test

# Source tarball for target-machine build/validation (F8) — delegates to
# cpp/Makefile's dist target, which shells out to tools/make_dist.py.
dist:
	$(MAKE) -C cpp dist
