#!/usr/bin/env python3
"""make_dist.py — F8 source tarball: `make dist` / `make -C cpp dist`
shell out here.

Builds dist/jnx-fh2-<YYYYMMDD>.tar.gz containing exactly what a target
box needs to build and run the C++ system plus the Python client/tools
— sources only, no prebuilt binaries, no git history, no test-only
Python (the prototype `jnxfeed` package is NOT included wholesale: only
the three stdlib-only files `jnxweb/__main__.py` actually imports at
runtime, `jnxfeed/__init__.py` + `jnxfeed/net/__init__.py` +
`jnxfeed/net/reactor.py`, are pulled in — see the DEPS comment below for
why that's the correct minimal set, not a guess).

Contents (all under one top-level `jnx-fh2-<date>/` directory):
    cpp/                    sources + Makefile (build/, build-asan/ excluded)
    jnxweb/                 production Python 3.6 client
    jnxfeed/__init__.py, jnxfeed/net/{__init__.py,reactor.py}
                            the one runtime dependency jnxweb has on the
                            Phase-1 package (its selectors-based reactor)
    tools/dbquery.py, mcast_spy.py, ws_probe.py, py36check.py
                            target-box operator/validation tools
    etc/                    sample config files
    docs/wire_spec.md
    OPERATIONS.md, RECOVERY.md, TARGET_VALIDATION.md
    tests/fixtures/sample_udp_head.itch
                            small fixture for the target-box dry run

Must build with only `make -C cpp all` and a C++11 compiler once
unpacked — no git, no python needed for the C++ half (JNX_PLAN2.md F8
boundary condition). Verify by extracting to a scratch dir and running
`make -C cpp all` there.
"""
import os
import shutil
import sys
import tarfile
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST_DIR = os.path.join(REPO_ROOT, "dist")

#: directory names never copied, anywhere in the tree (build artifacts,
#: caches — none of this belongs in a source tarball).
EXCLUDE_DIRS = frozenset(["build", "build-asan", "__pycache__", ".git"])
EXCLUDE_SUFFIXES = (".pyc", ".pyo")


def copy_tree_filtered(src, dst):
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        rel = os.path.relpath(root, src)
        dst_root = os.path.join(dst, rel) if rel != "." else dst
        os.makedirs(dst_root, exist_ok=True)
        for fn in files:
            if fn.endswith(EXCLUDE_SUFFIXES):
                continue
            shutil.copy2(os.path.join(root, fn), os.path.join(dst_root, fn))


def main():
    date_tag = time.strftime("%Y%m%d")
    name = "jnx-fh2-{}".format(date_tag)

    os.makedirs(DIST_DIR, exist_ok=True)
    staging_root = tempfile.mkdtemp(prefix="jnx-dist-")
    staging = os.path.join(staging_root, name)
    os.makedirs(staging)

    # --- cpp/ (sources + Makefile only) -------------------------------
    copy_tree_filtered(os.path.join(REPO_ROOT, "cpp"),
                       os.path.join(staging, "cpp"))

    # --- jnxweb/ (production Python client) ----------------------------
    copy_tree_filtered(os.path.join(REPO_ROOT, "jnxweb"),
                       os.path.join(staging, "jnxweb"))

    # --- jnxfeed's one runtime dependency for jnxweb --------------------
    os.makedirs(os.path.join(staging, "jnxfeed", "net"), exist_ok=True)
    shutil.copy2(os.path.join(REPO_ROOT, "jnxfeed", "__init__.py"),
                os.path.join(staging, "jnxfeed", "__init__.py"))
    shutil.copy2(os.path.join(REPO_ROOT, "jnxfeed", "net", "__init__.py"),
                os.path.join(staging, "jnxfeed", "net", "__init__.py"))
    shutil.copy2(os.path.join(REPO_ROOT, "jnxfeed", "net", "reactor.py"),
                os.path.join(staging, "jnxfeed", "net", "reactor.py"))

    # --- target-box tools -------------------------------------------------
    os.makedirs(os.path.join(staging, "tools"), exist_ok=True)
    for fn in ("dbquery.py", "mcast_spy.py", "ws_probe.py", "py36check.py"):
        shutil.copy2(os.path.join(REPO_ROOT, "tools", fn),
                    os.path.join(staging, "tools", fn))

    # --- configs ------------------------------------------------------------
    copy_tree_filtered(os.path.join(REPO_ROOT, "etc"),
                       os.path.join(staging, "etc"))

    # --- docs -----------------------------------------------------------
    os.makedirs(os.path.join(staging, "docs"), exist_ok=True)
    shutil.copy2(os.path.join(REPO_ROOT, "docs", "wire_spec.md"),
                os.path.join(staging, "docs", "wire_spec.md"))
    for fn in ("OPERATIONS.md", "RECOVERY.md", "TARGET_VALIDATION.md"):
        shutil.copy2(os.path.join(REPO_ROOT, fn),
                    os.path.join(staging, fn))

    # --- fixture for the target-box dry run --------------------------------
    os.makedirs(os.path.join(staging, "tests", "fixtures"), exist_ok=True)
    shutil.copy2(
        os.path.join(REPO_ROOT, "tests", "fixtures", "sample_udp_head.itch"),
        os.path.join(staging, "tests", "fixtures", "sample_udp_head.itch"))

    tarball = os.path.join(DIST_DIR, name + ".tar.gz")
    with tarfile.open(tarball, "w:gz") as tf:
        tf.add(staging, arcname=name)

    shutil.rmtree(staging_root, ignore_errors=True)

    size_mb = os.path.getsize(tarball) / (1024.0 * 1024.0)
    print("dist: wrote {} ({:.2f} MiB)".format(tarball, size_mb))
    return 0


if __name__ == "__main__":
    sys.exit(main())
