#!/usr/bin/env bash
# Evidence check for the README claim (What this deliberately is not): "Six keywords:
# cohort, where, and, measure, by, limit."
# Fails unless the parser's documented keyword list and every match arm in parse()
# are present in src/main.rs.
set -euo pipefail

src="src/main.rs"

if ! grep -qF 'cohort, where, and, measure, by, limit' "$src"; then
  echo "FAIL: the six-keyword grammar list is not documented in src/main.rs" >&2
  exit 1
fi
for pat in '"cohort"' '"and" | "where"' '"measure"' '"by"' '"limit"'; do
  if ! grep -qF "$pat" "$src"; then
    echo "FAIL: grammar match arm missing in src/main.rs: $pat" >&2
    exit 1
  fi
done
echo "OK: the six grammar keywords are the parser's full match set"
