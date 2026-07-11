# cpp/Makefile — plain GNU make, no recursion, no cmake.
#
# Targets:
#   all    - build all libraries/binaries (currently just prepares build dir;
#            later phases add jnxfh/jnxdb/tools binaries as BINS grows)
#   test   - build every cpp/test/test_*.cpp into cpp/build/ and run each;
#            fails (non-zero exit) if any test binary fails or any build fails
#   clean  - remove cpp/build/
#
# Adding a later main binary (e.g. jnxfh) should just mean adding its .cpp
# sources to a SRCS_jnxfh list and a rule/target following the pattern used
# for tools below — the test/build machinery here does not need to change.

CXX ?= g++
CXXFLAGS = -std=c++11 -Wall -Wextra -Werror -pedantic -O2 -g -pthread
LDFLAGS = -pthread

ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
BUILD := $(ROOT)/build

# Directories later phases will add sources under (kept here so `all`
# has an obvious place to grow into without restructuring the Makefile).
COMMON_DIR := $(ROOT)/common
TOOLS_DIR := $(ROOT)/tools
TEST_DIR := $(ROOT)/test

# Module ("library") sources shared by tools, main binaries and tests.
# Future phases (market/, wire/, db/, fh/) just extend MOD_DIRS; main
# binaries get their own target following the itch_replay pattern below.
MOD_DIRS := itch soup market wire db fh
# *_main.cpp files define main() for a process binary and must not be
# linked into tests/tools — they get their own binary targets below.
MOD_SRCS := $(filter-out %_main.cpp,$(foreach d,$(MOD_DIRS),$(wildcard $(ROOT)/$(d)/*.cpp)))
MOD_OBJS := $(patsubst $(ROOT)/%.cpp,$(BUILD)/%.o,$(MOD_SRCS))

# Conservative header dependency: any header change rebuilds everything.
# Fine at this codebase size; swap for -MMD if it ever gets slow.
HDRS := $(wildcard $(ROOT)/*/*.h)

BINS := $(BUILD)/itch_replay $(BUILD)/book_dump $(BUILD)/gen_record_vectors \
        $(BUILD)/jnxdb $(BUILD)/jnxfh

.PHONY: all test clean

all: $(BINS) | $(BUILD)

$(BUILD):
	mkdir -p $(BUILD)

# Pattern rule: build any object from a .cpp anywhere under cpp/, mirroring
# the relative path under build/ so future subdirs (itch/, market/, ...)
# don't collide.
$(BUILD)/%.o: $(ROOT)/%.cpp $(HDRS) | $(BUILD)
	mkdir -p $(dir $@)
	$(CXX) $(CXXFLAGS) -I$(ROOT) -c -o $@ $<

# --- tools --------------------------------------------------------------

$(BUILD)/itch_replay: $(BUILD)/tools/itch_replay.o $(MOD_OBJS) | $(BUILD)
	$(CXX) $(CXXFLAGS) -o $@ $(BUILD)/tools/itch_replay.o $(MOD_OBJS) $(LDFLAGS)

$(BUILD)/book_dump: $(BUILD)/tools/book_dump.o $(MOD_OBJS) | $(BUILD)
	$(CXX) $(CXXFLAGS) -o $@ $(BUILD)/tools/book_dump.o $(MOD_OBJS) $(LDFLAGS)

$(BUILD)/gen_record_vectors: $(BUILD)/tools/gen_record_vectors.o $(MOD_OBJS) | $(BUILD)
	$(CXX) $(CXXFLAGS) -o $@ $(BUILD)/tools/gen_record_vectors.o $(MOD_OBJS) $(LDFLAGS)

# --- process binaries ----------------------------------------------------

$(BUILD)/jnxdb: $(BUILD)/db/jnxdb_main.o $(MOD_OBJS) | $(BUILD)
	$(CXX) $(CXXFLAGS) -o $@ $(BUILD)/db/jnxdb_main.o $(MOD_OBJS) $(LDFLAGS)

$(BUILD)/jnxfh: $(BUILD)/fh/jnxfh_main.o $(MOD_OBJS) | $(BUILD)
	$(CXX) $(CXXFLAGS) -o $@ $(BUILD)/fh/jnxfh_main.o $(MOD_OBJS) $(LDFLAGS)

# --- test harness -----------------------------------------------------

TEST_SRCS := $(wildcard $(TEST_DIR)/test_*.cpp)
TEST_BINS := $(patsubst $(TEST_DIR)/%.cpp,$(BUILD)/test/%,$(TEST_SRCS))

$(BUILD)/test/%: $(TEST_DIR)/%.cpp $(MOD_OBJS) $(HDRS) | $(BUILD)
	mkdir -p $(dir $@)
	$(CXX) $(CXXFLAGS) -I$(ROOT) -o $@ $< $(MOD_OBJS) $(LDFLAGS)

test: $(TEST_BINS)
	@for t in $(TEST_BINS); do \
		echo "=== running $$t ==="; \
		$$t || exit 1; \
	done

# --- ASAN/UBSAN gate (F8) -------------------------------------------------
#
# Entirely separate build tree (build-asan/) and flags var so this never
# fights with -Werror on the -O2 production build. -O1 keeps stack traces
# useful without being unusably slow. No -Werror here on purpose: ASAN's
# own instrumentation output isn't something we want to fail the build on
# if a newer compiler adds a pedantic warning inside sanitizer glue.
BUILD_ASAN := $(ROOT)/build-asan
ASAN_CXXFLAGS = -std=c++11 -Wall -Wextra -pedantic -O1 -g -pthread \
                -fsanitize=address,undefined -fno-omit-frame-pointer
ASAN_LDFLAGS = -pthread -fsanitize=address,undefined

MOD_OBJS_ASAN := $(patsubst $(ROOT)/%.cpp,$(BUILD_ASAN)/%.o,$(MOD_SRCS))

$(BUILD_ASAN):
	mkdir -p $(BUILD_ASAN)

$(BUILD_ASAN)/%.o: $(ROOT)/%.cpp $(HDRS) | $(BUILD_ASAN)
	mkdir -p $(dir $@)
	$(CXX) $(ASAN_CXXFLAGS) -I$(ROOT) -c -o $@ $<

$(BUILD_ASAN)/jnxdb: $(BUILD_ASAN)/db/jnxdb_main.o $(MOD_OBJS_ASAN) | $(BUILD_ASAN)
	$(CXX) $(ASAN_CXXFLAGS) -o $@ $(BUILD_ASAN)/db/jnxdb_main.o $(MOD_OBJS_ASAN) $(ASAN_LDFLAGS)

$(BUILD_ASAN)/jnxfh: $(BUILD_ASAN)/fh/jnxfh_main.o $(MOD_OBJS_ASAN) | $(BUILD_ASAN)
	$(CXX) $(ASAN_CXXFLAGS) -o $@ $(BUILD_ASAN)/fh/jnxfh_main.o $(MOD_OBJS_ASAN) $(ASAN_LDFLAGS)

TEST_BINS_ASAN := $(patsubst $(TEST_DIR)/%.cpp,$(BUILD_ASAN)/test/%,$(TEST_SRCS))

$(BUILD_ASAN)/test/%: $(TEST_DIR)/%.cpp $(MOD_OBJS_ASAN) $(HDRS) | $(BUILD_ASAN)
	mkdir -p $(dir $@)
	$(CXX) $(ASAN_CXXFLAGS) -I$(ROOT) -o $@ $< $(MOD_OBJS_ASAN) $(ASAN_LDFLAGS)

test-asan: $(TEST_BINS_ASAN) $(BUILD_ASAN)/jnxdb $(BUILD_ASAN)/jnxfh
	@for t in $(TEST_BINS_ASAN); do \
		echo "=== running (asan) $$t ==="; \
		ASAN_OPTIONS=detect_leaks=1 UBSAN_OPTIONS=print_stacktrace=1 $$t || exit 1; \
	done
	@echo "=== asan pipeline: sim -> jnxfh -> jnxdb over the head fixture ==="
	cd $(ROOT)/.. && \
		JNXDB_BIN=$(BUILD_ASAN)/jnxdb JNXFH_BIN=$(BUILD_ASAN)/jnxfh \
		ASAN_OPTIONS=detect_leaks=1 UBSAN_OPTIONS=print_stacktrace=1 \
		python3 tools/run_asan_pipeline.py

# Bonus (plan F8 step 2): if valgrind is present, offer it too. ASAN
# remains the actual gate (see test-asan) — this target is best-effort.
test-valgrind: $(TEST_BINS)
	@if ! command -v valgrind >/dev/null 2>&1; then \
		echo "test-valgrind: valgrind not installed, skipping"; \
		exit 0; \
	fi; \
	for t in $(TEST_BINS); do \
		echo "=== valgrind $$t ==="; \
		valgrind --leak-check=full --error-exitcode=1 -q $$t || exit 1; \
	done

# --- benchmark (F8) --------------------------------------------------------
#
# decode-only (itch_replay), decode+apply (book_dump), and full pipeline
# (sim -> jnxfh -> jnxdb + multicast) over the full official sample.
# Excludes sim pacing (max speed) and, as far as practical, process
# startup. See tools/bench.py for methodology notes.
bench: all
	cd $(ROOT)/.. && python3 tools/bench.py

# --- source distribution (F8) ----------------------------------------------
#
# Tarball built FROM SOURCE on the target — no prebuilt binaries shipped.
# Must build with only `make -C cpp all` and a C++11 compiler (gcc-c++ +
# make, nothing else) once extracted. See tools/make_dist.py.
dist:
	cd $(ROOT)/.. && python3 tools/make_dist.py

clean:
	rm -rf $(BUILD) $(BUILD_ASAN)

.PHONY: test-asan test-valgrind bench dist
