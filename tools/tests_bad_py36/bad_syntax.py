"""Fixture: 3.7+ syntax forms (walrus, posonly params, match, f-string '=')."""


def f(a, b, /, c):  # BAD: posonly
    return a + b + c


def g(items):
    if (n := len(items)) > 5:  # BAD: walrus
        return n
    return 0


def h(x):
    match x:  # BAD: match
        case 1:
            return "one"
        case _:
            return "other"


def show(x):
    print(f"{x=}")  # BAD: fstring_eq
    print(f"{x = }")  # BAD: fstring_eq
    print(f"{x=:.2f}")  # BAD: fstring_eq
