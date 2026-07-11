#!/usr/bin/env python3
"""Python-3.6 compatibility linter (F0.4 / JNX_PLAN2.md).

Runs on the dev interpreter (3.14) but MUST itself be valid, clean
Python 3.6 source, because it later ships to and runs on the RHEL 8
target (platform python 3.6). Do not use any syntax or stdlib feature
newer than 3.6 in this file, ever.

Usage:
    python3 tools/py36check.py FILE [FILE ...]
    python3 tools/py36check.py --selftest

Exit code 0 if every given file is 3.6-clean; exit 1 and print one
``file:line: reason`` line per violation otherwise.

Detection strategy
-------------------
Most rules are detected precisely via the ``ast`` module: on 3.14 the
parser accepts (and represents) every syntax form we need to reject
(walrus, positional-only params, ``match``, f-string self-documenting
``=``) because 3.14's *grammar* is a superset of 3.6's. The one AST
subtlety is the f-string ``=`` specifier: CPython represents
``f"{x=}"`` as a ``JoinedStr`` whose values are a string ``Constant``
ending in ``"="`` (or ``"x = "``, if the source has spaces) directly
followed by the corresponding ``FormattedValue`` -- this checker keys
off exactly that shape, which is precise (no regex needed for it).

A small supplementary plain-text pass exists for two of the "forbidden
call" rules (``breakpoint(`` and ``time.time_ns(``) as a textual
backstop alongside the AST-based check -- it only matches on clear,
whole-token occurrences outside of the AST walk's own reasoning, so it
adds recall without meaningfully adding false positives on real code
(it can double-report a line the AST pass already caught; violations
are de-duplicated by (file, line, reason)).
"""
import argparse
import ast
import io
import os
import re
import sys
import tokenize

FORBIDDEN_IMPORTS = {
    "dataclasses": "'dataclasses' module not available before Python 3.7",
    "asyncio": "'asyncio' is forbidden by project rules (no asyncio at all)",
    "contextvars": "'contextvars' module not available before Python 3.7",
    "zoneinfo": "'zoneinfo' module not available before Python 3.9",
    "graphlib": "'graphlib' module not available before Python 3.9",
    "importlib.metadata": "'importlib.metadata' not available before Python 3.8",
}

FORBIDDEN_TYPING_NAMES = {
    "Literal": "typing.Literal not available before Python 3.8",
    "Protocol": "typing.Protocol not available before Python 3.8",
    "Final": "typing.Final not available before Python 3.8",
}

SUBPROCESS_FUNCS = {"run", "check_output", "Popen"}
SUBPROCESS_BAD_KWARGS = {
    "capture_output": (
        "subprocess ... capture_output= kwarg not available before Python 3.7"
    ),
    "text": "subprocess ... text= kwarg not available before Python 3.7",
}

# Plain-text backstop pass: whole-token regexes only, applied to raw
# source lines. Kept deliberately narrow to avoid false positives on
# comments/strings mentioning these words in prose.
_TEXT_RULES = [
    (re.compile(r"(?<![\w.])breakpoint\s*\("), "breakpoint() not available before Python 3.7"),
    (re.compile(r"(?<![\w])time\.time_ns\s*\("), "time.time_ns() not available before Python 3.7"),
]


class Violation(object):
    __slots__ = ("line", "reason")

    def __init__(self, line, reason):
        self.line = line
        self.reason = reason


class Py36Checker(ast.NodeVisitor):
    """Walks one module's AST, collecting Violation objects."""

    def __init__(self, source):
        self.source = source
        self.violations = []
        self._typing_aliases = set()
        self._subprocess_aliases = set()
        self._subprocess_func_names = set()

    # -- helpers ----------------------------------------------------

    def _add(self, node, reason):
        line = getattr(node, "lineno", 0)
        self.violations.append(Violation(line, reason))

    def _prescan_aliases(self, tree):
        """First pass: learn import aliases so attribute-usage checks work."""
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    local = alias.asname or alias.name
                    if alias.name == "typing":
                        self._typing_aliases.add(local)
                    if alias.name == "subprocess":
                        self._subprocess_aliases.add(local)
            elif isinstance(node, ast.ImportFrom):
                if node.module == "subprocess":
                    for alias in node.names:
                        if alias.name in SUBPROCESS_FUNCS:
                            self._subprocess_func_names.add(alias.asname or alias.name)

    # -- syntax-shape rules -------------------------------------------

    def visit_NamedExpr(self, node):  # walrus operator, 3.8+
        self._add(node, "walrus operator (:=) not allowed before Python 3.8")
        self.generic_visit(node)

    def visit_Match(self, node):  # match statement, 3.10+
        self._add(node, "match statement not allowed before Python 3.10")
        self.generic_visit(node)

    def visit_JoinedStr(self, node):  # f-string; check for '=' self-doc specifier
        values = node.values
        for i, value in enumerate(values):
            if (
                isinstance(value, ast.Constant)
                and isinstance(value.value, str)
                and value.value.rstrip().endswith("=")
                and i + 1 < len(values)
                and isinstance(values[i + 1], ast.FormattedValue)
            ):
                self._add(
                    value,
                    "f-string self-documenting '=' specifier not allowed before Python 3.8",
                )
        self.generic_visit(node)

    def visit_arguments(self, node):  # positional-only params, 3.8+
        if getattr(node, "posonlyargs", None):
            self._add(
                node.posonlyargs[0],
                "positional-only parameters ('/') not allowed before Python 3.8",
            )
        self.generic_visit(node)

    # -- imports --------------------------------------------------------

    def visit_Import(self, node):
        for alias in node.names:
            reason = FORBIDDEN_IMPORTS.get(alias.name)
            if reason:
                self._add(node, "{} (import {})".format(reason, alias.name))
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        module = node.module or ""
        if module == "__future__":
            for alias in node.names:
                if alias.name == "annotations":
                    self._add(
                        node,
                        "'from __future__ import annotations' not allowed "
                        "(PEP 563 stringized annotations are a 3.7+ feature "
                        "and change runtime annotation semantics)",
                    )
        reason = FORBIDDEN_IMPORTS.get(module)
        if reason:
            self._add(node, "{} (from {} import ...)".format(reason, module))
        if module == "importlib":
            for alias in node.names:
                if alias.name == "metadata":
                    self._add(
                        alias if hasattr(alias, "lineno") else node,
                        "{} (from importlib import metadata)".format(
                            FORBIDDEN_IMPORTS["importlib.metadata"]
                        ),
                    )
        if module == "typing":
            for alias in node.names:
                treason = FORBIDDEN_TYPING_NAMES.get(alias.name)
                if treason:
                    self._add(alias if hasattr(alias, "lineno") else node, treason)
        self.generic_visit(node)

    # -- calls ------------------------------------------------------------

    def visit_Call(self, node):
        func = node.func
        # breakpoint()
        if isinstance(func, ast.Name) and func.id == "breakpoint":
            self._add(node, "breakpoint() not available before Python 3.7")
        # time.time_ns()
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "time_ns"
            and isinstance(func.value, ast.Name)
            and func.value.id == "time"
        ):
            self._add(node, "time.time_ns() not available before Python 3.7")
        # typing.Literal / .Protocol / .Final used as a call, e.g. typing.Final(...)
        self._check_typing_attr(func)
        # subprocess.run/check_output/Popen(..., capture_output=..., text=...)
        is_subprocess_call = False
        if (
            isinstance(func, ast.Attribute)
            and func.attr in SUBPROCESS_FUNCS
            and isinstance(func.value, ast.Name)
            and func.value.id in self._subprocess_aliases
        ):
            is_subprocess_call = True
        elif isinstance(func, ast.Name) and func.id in self._subprocess_func_names:
            is_subprocess_call = True
        if is_subprocess_call:
            for kw in node.keywords:
                if kw.arg in SUBPROCESS_BAD_KWARGS:
                    self._add(node, SUBPROCESS_BAD_KWARGS[kw.arg])
        self.generic_visit(node)

    def visit_Attribute(self, node):
        self._check_typing_attr(node)
        self.generic_visit(node)

    def visit_Name(self, node):
        # bare use of a name imported directly from typing, e.g.
        # "from typing import Literal" then "x: Literal['a']" -- the
        # import itself is already flagged in visit_ImportFrom, so this
        # is intentionally not duplicated here.
        self.generic_visit(node)

    def _check_typing_attr(self, node):
        if (
            isinstance(node, ast.Attribute)
            and node.attr in FORBIDDEN_TYPING_NAMES
            and isinstance(node.value, ast.Name)
            and node.value.id in self._typing_aliases
        ):
            self._add(node, FORBIDDEN_TYPING_NAMES[node.attr])


def _mask_strings_and_comments(source):
    """Blank out STRING/COMMENT token text, keeping line/col layout intact.

    The plain-text backstop pass (_TEXT_RULES) must only match real code,
    not prose inside comments/docstrings that happens to mention a
    forbidden token (this file's own reason strings are exactly such
    prose) -- otherwise it would false-positive on itself and on any
    other file that documents these rules in a comment.
    """
    lines = source.splitlines(True)
    masked = [list(line) for line in lines]
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for tok in tokens:
            if tok.type not in (tokenize.STRING, tokenize.COMMENT):
                continue
            (srow, scol), (erow, ecol) = tok.start, tok.end
            for row in range(srow, erow + 1):
                if row - 1 >= len(masked):
                    continue
                line = masked[row - 1]
                col_start = scol if row == srow else 0
                col_end = ecol if row == erow else len(line)
                for col in range(col_start, min(col_end, len(line))):
                    if line[col] != "\n":
                        line[col] = " "
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # Fall back to unmasked source; the AST pass already handles
        # real syntax errors, this is only the supplementary text pass.
        pass
    return ["".join(line) for line in masked]


def _dedup(violations):
    seen = set()
    out = []
    for v in violations:
        key = (v.line, v.reason)
        if key in seen:
            continue
        seen.add(key)
        out.append(v)
    return out


def check_source(source, filename="<string>"):
    """Return a sorted list of Violation for one file's source text."""
    violations = []
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as exc:
        return [Violation(exc.lineno or 0, "SyntaxError: {}".format(exc.msg))]

    checker = Py36Checker(source)
    checker._prescan_aliases(tree)
    checker.visit(tree)
    violations.extend(checker.violations)

    masked_lines = _mask_strings_and_comments(source)
    for lineno, line in enumerate(masked_lines, start=1):
        for pattern, reason in _TEXT_RULES:
            if pattern.search(line):
                violations.append(Violation(lineno, reason))

    violations = _dedup(violations)
    violations.sort(key=lambda v: v.line)
    return violations


def check_file(path):
    with open(path, "r", encoding="utf-8") as f:
        source = f.read()
    return check_source(source, filename=path)


def _run_selftest():
    fixtures_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests_bad_py36")
    if not os.path.isdir(fixtures_dir):
        sys.stderr.write("selftest: fixtures dir not found: {}\n".format(fixtures_dir))
        return 1

    ok = True
    bad_marker = re.compile(r"#\s*BAD:")

    for name in sorted(os.listdir(fixtures_dir)):
        if not name.endswith(".py"):
            continue
        path = os.path.join(fixtures_dir, name)
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        expected_lines = set(
            i + 1 for i, line in enumerate(lines) if bad_marker.search(line)
        )
        violations = check_file(path)
        found_lines = set(v.line for v in violations)

        if name == "clean.py":
            if violations:
                ok = False
                sys.stderr.write(
                    "selftest FAIL: {} expected clean, found {} violation(s):\n".format(
                        name, len(violations)
                    )
                )
                for v in violations:
                    sys.stderr.write("  {}:{}: {}\n".format(name, v.line, v.reason))
            else:
                print("selftest OK: {} clean as expected".format(name))
            continue

        missing = expected_lines - found_lines
        if missing:
            ok = False
            sys.stderr.write(
                "selftest FAIL: {} missing violations at line(s) {}\n".format(
                    name, sorted(missing)
                )
            )
        else:
            print(
                "selftest OK: {} — {} planted violation(s) all caught".format(
                    name, len(expected_lines)
                )
            )

    return 0 if ok else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="Python-3.6 compatibility linter")
    parser.add_argument("files", nargs="*", help="files to check")
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="run the checker over tools/tests_bad_py36/ and verify coverage",
    )
    args = parser.parse_args(argv)

    if args.selftest:
        return _run_selftest()

    if not args.files:
        parser.error("no files given (or use --selftest)")

    any_violations = False
    for path in args.files:
        violations = check_file(path)
        if violations:
            any_violations = True
            for v in violations:
                print("{}:{}: {}".format(path, v.line, v.reason))

    return 1 if any_violations else 0


if __name__ == "__main__":
    sys.exit(main())
