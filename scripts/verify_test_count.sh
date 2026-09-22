#!/usr/bin/env bash
# Evidence check for the README Build section's test count ("cargo test" reports N tests).
# Fails unless the number of #[test] functions in src/main.rs equals the stated count.
set -euo pipefail

expected=10

n="$(grep -c '#\[test\]' src/main.rs)"
if [ "$n" -ne "$expected" ]; then
  echo "FAIL: README states ${expected} tests, found $n #[test] functions in src/main.rs" >&2
  exit 1
fi
echo "OK: ${expected} #[test] functions in src/main.rs"
