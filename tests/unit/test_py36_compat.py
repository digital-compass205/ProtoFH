"""Guard against Python-3.7+ features creeping into runtime code.

The deployment target is Python 3.6.4 (RHEL 8.10) and development often
happens on newer interpreters, where 3.7+ idioms run fine and go unnoticed.
This test statically scans every module under jnxfeed/ for the constructs
forbidden by JNX_PLAN.md section 0. It is a safety net, not a substitute
for `make test-docker` (pytest on real 3.6 in the UBI8 container).
"""
import ast
import pathlib

PACKAGE_ROOT = pathlib.Path(__file__).resolve().parents[2] / "jnxfeed"

FORBIDDEN_IMPORTS = {
    "dataclasses": "use typing.NamedTuple or __slots__ classes",
    "asyncio": "use the selectors reactor (jnxfeed.net.reactor)",
}

FORBIDDEN_CALL_KWARGS = {
    # subprocess.run/Popen kwargs that don't exist on 3.6
    "capture_output": "use stdout=PIPE, stderr=PIPE",
    "text": "use universal_newlines=True",
}


def iter_sources():
    files = sorted(PACKAGE_ROOT.rglob("*.py"))
    assert files, "no sources found under {}".format(PACKAGE_ROOT)
    return files


def test_no_forbidden_constructs():
    problems = []
    for path in iter_sources():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "").split(".")[0]]
            else:
                names = []
            for name in names:
                if name in FORBIDDEN_IMPORTS:
                    problems.append(
                        "{}:{}: import {} — {}".format(
                            path, node.lineno, name, FORBIDDEN_IMPORTS[name]
                        )
                    )

            # Walrus operator (3.8+)
            if isinstance(node, ast.NamedExpr):
                problems.append(
                    "{}:{}: walrus operator := is 3.8+".format(path, node.lineno)
                )

            # 3.7+ subprocess kwargs
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg in FORBIDDEN_CALL_KWARGS:
                        problems.append(
                            "{}:{}: kwarg {}= — {}".format(
                                path, node.lineno, kw.arg,
                                FORBIDDEN_CALL_KWARGS[kw.arg],
                            )
                        )

            # `from __future__ import annotations` (3.7+)
            if isinstance(node, ast.ImportFrom) and node.module == "__future__":
                for alias in node.names:
                    if alias.name == "annotations":
                        problems.append(
                            "{}:{}: from __future__ import annotations is 3.7+".format(
                                path, node.lineno
                            )
                        )

    assert not problems, "Python 3.6 compatibility violations:\n" + "\n".join(problems)
