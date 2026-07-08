# Prefer a real 3.6 interpreter when present (the deployment target);
# otherwise fall back to whatever python3 is available for a fast local run.
PY := $(shell command -v python3.6 2>/dev/null || command -v python3)

.PHONY: test test-docker

test:
	$(PY) -m pytest -q

# Authoritative run: Python 3.6 inside the RHEL 8 (UBI8) container.
test-docker:
	docker build -f Dockerfile.dev -t jnxfeed-test .
	docker run --rm jnxfeed-test
