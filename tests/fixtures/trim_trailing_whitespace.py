"""Tiny offline pre-commit fixer used by test_preflight.py: strips trailing
whitespace from each file passed on argv, in place."""
import sys

for path in sys.argv[1:]:
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    trimmed = "\n".join(line.rstrip() for line in text.splitlines())
    if text.endswith("\n"):
        trimmed += "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(trimmed)
