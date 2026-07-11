"""Fixture: a file with no planted 3.6-incompatibility violations."""
import os
import sys


def add(a, b):
    return a + b


class Point(object):
    def __init__(self, x, y):
        self.x = x
        self.y = y

    def __repr__(self):
        return "Point(x={}, y={})".format(self.x, self.y)


def main():
    p = Point(1, 2)
    print(p)
    print(os.getcwd())
    return sys.exit(0)


if __name__ == "__main__":
    main()
