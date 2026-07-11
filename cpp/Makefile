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

# Header-only common/ for now: nothing to compile into objects yet.
# Future phases with .cpp files (itch/, market/, wire/, db/, fh/) add
# their own SRCS_* variables and object rules following this pattern:
#   SRCS_jnxfh := $(wildcard $(ROOT)/fh/*.cpp) $(wildcard $(ROOT)/itch/*.cpp) ...
#   OBJS_jnxfh := $(patsubst $(ROOT)/%.cpp,$(BUILD)/%.o,$(SRCS_jnxfh))
#   $(BUILD)/jnxfh: $(OBJS_jnxfh) | $(BUILD)
#	$(CXX) $(CXXFLAGS) -o $@ $(OBJS_jnxfh) $(LDFLAGS)

BINS :=

.PHONY: all test clean

all: $(BINS) | $(BUILD)

$(BUILD):
	mkdir -p $(BUILD)

# Pattern rule: build any object from a .cpp anywhere under cpp/, mirroring
# the relative path under build/ so future subdirs (itch/, market/, ...)
# don't collide.
$(BUILD)/%.o: $(ROOT)/%.cpp | $(BUILD)
	mkdir -p $(dir $@)
	$(CXX) $(CXXFLAGS) -I$(ROOT) -c -o $@ $<

# --- test harness -----------------------------------------------------

TEST_SRCS := $(wildcard $(TEST_DIR)/test_*.cpp)
TEST_BINS := $(patsubst $(TEST_DIR)/%.cpp,$(BUILD)/test/%,$(TEST_SRCS))

$(BUILD)/test/%: $(TEST_DIR)/%.cpp | $(BUILD)
	mkdir -p $(dir $@)
	$(CXX) $(CXXFLAGS) -I$(ROOT) -o $@ $< $(LDFLAGS)

test: $(TEST_BINS)
	@for t in $(TEST_BINS); do \
		echo "=== running $$t ==="; \
		$$t || exit 1; \
	done

clean:
	rm -rf $(BUILD)
