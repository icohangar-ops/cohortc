#!/usr/bin/env bash
# Evidence check for the README claim: "Pure Rust, two dependencies, no LLM."
# Fails unless [dependencies] in Cargo.toml contains exactly serde and serde_yaml
# and no LLM/network SDK appears in the manifest.
#
# grep -c (never -q) so every pipeline reads to EOF: grep -q exits on first match
# and, under set -o pipefail, turns a writer SIGPIPE into a spurious failure.
set -euo pipefail

deps="$(awk '/^\[dependencies\]/{f=1; next} /^\[/{f=0} f && NF && $0 !~ /^#/ {print $1}' Cargo.toml)"

count="$(printf '%s\n' "$deps" | grep -c . || true)"
if [ "$count" -ne 2 ]; then
  echo "FAIL: expected exactly 2 [dependencies] entries, found $count. Lines seen:" >&2
  printf '%s\n' "$deps" >&2
  exit 1
fi
for want in serde serde_yaml; do
  n="$(printf '%s\n' "$deps" | grep -cx "$want" || true)"
  if [ "$n" -ne 1 ]; then
    echo "FAIL: expected dependency '$want' exactly once in Cargo.toml [dependencies]. Lines seen:" >&2
    printf '%s\n' "$deps" >&2
    exit 1
  fi
done
for bad in openai anthropic llm langchain reqwest ureq hyper tokio curl; do
  n="$(printf '%s\n' "$deps" | grep -ci "^$bad" || true)"
  if [ "$n" -ne 0 ]; then
    echo "FAIL: unexpected LLM/network dependency '$bad' in Cargo.toml [dependencies]" >&2
    exit 1
  fi
done
echo "OK: exactly two dependencies (serde, serde_yaml); no LLM/network SDK in the manifest"
