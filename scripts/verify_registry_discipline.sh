#!/usr/bin/env bash
# Evidence check for the README claim (The registry): "One YAML file. Every countable
# population is named here, once, with an explicit predicate."
# Fails unless registry/ holds exactly one YAML file and every cohort in it declares
# an entity and at least one predicate.
set -euo pipefail

reg="registry/cohorts.yaml"
if [ ! -f "$reg" ]; then
  echo "FAIL: $reg missing" >&2
  exit 1
fi

n_yaml="$(find registry -maxdepth 1 -type f \( -name '*.yaml' -o -name '*.yml' \) | wc -l)"
if [ "$n_yaml" -ne 1 ]; then
  echo "FAIL: expected exactly one registry YAML under registry/, found $n_yaml" >&2
  exit 1
fi

python3 - "$reg" <<'PY'
import sys

section = None
current = None
cohorts = {}
with open(sys.argv[1], encoding="utf-8") as handle:
    for raw in handle:
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        key = line.strip().split(":", 1)[0]
        if indent == 0:
            section = key
            current = None
        elif indent == 2 and section == "cohorts":
            current = key
            cohorts[current] = {"entity": False, "predicates": 0}
        elif current is not None and section == "cohorts":
            if key == "entity":
                cohorts[current]["entity"] = True
            elif key == "predicate":
                cohorts[current]["predicates"] += 1

missing = [c for c, v in sorted(cohorts.items()) if not v["entity"] or v["predicates"] < 1]
if missing:
    print(f"FAIL: cohorts without an entity or an explicit predicate: {', '.join(missing)}", file=sys.stderr)
    sys.exit(1)
print(f"OK: {len(cohorts)} cohorts, each with an entity and an explicit predicate, in one registry YAML")
PY
