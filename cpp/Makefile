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

clean:
	rm -rf $(BUILD)
