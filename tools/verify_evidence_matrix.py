#!/usr/bin/env python3
"""Cubiczan evidence-matrix verifier — canonical standard-kit implementation.

Machine-verifies every capability claim declared in a repo's
``evidence/matrix.yaml``: each claim row binds one stated claim to one or more
deterministic evidence refs (a test, a script, a manifest field, or a hashed
artifact), and this verifier refuses — fail-closed, on every run — while any
row is unverifiable.

Spec: "Evidence Matrix Requirement — Data-Cluster Deterministic Verification"
(schema v1; exactly four evidence types; no skip flags, no allowlists, no
quiet modes — there is no invocation of this tool that passes an unverified
repo).

Usage (in a repo that vendors a stamped copy at ``tools/verify_evidence_matrix.py``):

    python3 tools/verify_evidence_matrix.py                # verify <repo-root>/evidence/matrix.yaml
    python3 tools/verify_evidence_matrix.py --repo-root .  # explicit repo root
    python3 tools/verify_evidence_matrix.py --matrix PATH  # explicit matrix path

The repo root defaults to the directory that contains the ``tools/`` directory
this file sits in (the vendored layout), falling back to the current working
directory when the file is run from anywhere else.

Exit codes
----------
0   every claim verified (``EVIDENCE MATRIX: VERIFIED (n/n)``)
1   the matrix was interpretable but at least one refusal rule fired:
    zero-evidence rows, duplicate ids, unknown evidence types, refs whose
    target does not exist, hash or field-value mismatch, a source locator
    pointing at a file that does not exist, unknown schema fields, or a
    matrix that declares no claims at all
2   the gate cannot run at all: missing matrix file, malformed YAML, a
    non-mapping document, or a ``schema_version`` that is not exactly 1

The verifier is stdlib-only Python 3, performs no network access, and never
invokes git. ``test`` rows are checked for static existence (the referenced
test file — and for pytest node ids, the test function — must exist in the
tree); the repo's normal CI test job is what actually executes them.
``script`` rows are executed here, in-repo, with a hard timeout, and must
exit 0.
"""
# Canonical source: icohangar-ops/consensus-hardening-protocol · tools/verify_evidence_matrix.py
# Canonical kit commit: pinned post-merge via `git log -1 --follow -- tools/verify_evidence_matrix.py`
# on main (introduced as ceb4831114d31a5c97ab2225242d93abc443fc29 in _cubiczan-shared, archived).
import argparse
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

try:
    import yaml as _pyyaml  # optional; the vendored fallback covers bare environments
except ImportError:  # pragma: no cover - exercised via CI's no-PyYAML leg
    _pyyaml = None

EVIDENCE_MATRIX_VERIFIER_VERSION = "1.0.0"

SCHEMA_VERSION = 1
DEFAULT_SCRIPT_TIMEOUT_SECONDS = 120
DEFAULT_MATRIX_RELPATH = ("evidence", "matrix.yaml")

EVIDENCE_TYPES = ("test", "script", "manifest_field", "artifact_hash")

# Closed runner vocabulary for runner-prefixed ``test`` refs: prefix -> a manifest
# file that must exist somewhere in the tree. A new runner is a kit-level schema
# extension, not a per-repo freedom.
TEST_RUNNERS = {
    "cargo-test": "Cargo.toml",
    "npm-test": "package.json",
    "go-test": "go.mod",
}

TOP_LEVEL_KEYS = {"schema_version", "repo", "claims"}
CLAIM_KEYS = {"id", "claim", "source", "evidence"}
EVIDENCE_KEYS = {
    "test": {"type", "ref"},
    "script": {"type", "ref", "timeout_seconds"},
    "manifest_field": {"type", "ref", "equals"},
    "artifact_hash": {"type", "ref", "sha256"},
}

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_UNUSABLE = 2

# Directories that are never part of the verifiable tree (VCS internals, caches).
_SKIPPED_DIRS = {".git", ".hg", ".svn", "__pycache__", ".venv", "venv", "node_modules"}


class UnusableMatrix(Exception):
    """The gate cannot run at all (missing/malformed matrix, bad schema)."""


class YamlSubsetError(Exception):
    """Raised by the fallback parser on input outside the supported subset."""


# ---------------------------------------------------------------------------
# YAML loading — PyYAML when importable, else a minimal block-YAML subset
# ---------------------------------------------------------------------------

def _strip_comment(line: str) -> str:
    """Drop a trailing/full-line ``#`` comment, honoring quoted strings."""
    quote = None
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            if quote == '"' and ch == "\\":
                i += 1  # skip the escaped character
            elif ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i].rstrip()
        i += 1
    return line.rstrip()


def _prep_lines(text: str):
    """Return [(indent, content, lineno)] for non-blank, non-comment lines."""
    out = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        leading = raw[: len(raw) - len(raw.lstrip())]
        if "\t" in leading:
            raise YamlSubsetError(f"line {lineno}: tab indentation is not supported")
        stripped = _strip_comment(raw)
        if not stripped.strip():
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        out.append((indent, stripped.strip(), lineno))
    return out


def load_yaml(text: str) -> Any:
    """Parse YAML with PyYAML when available, else the minimal subset parser."""
    if _pyyaml is not None:
        return _pyyaml.safe_load(text)
    return parse_yaml_subset(text)


def parse_yaml_subset(text: str) -> Any:
    """Parse the block-YAML subset sufficient for schema-v1 matrices.

    Supported: nested mappings, lists of scalars/mappings, one-line flow
    mappings ``{k: v}`` and sequences ``[a, b]``, block scalars ``|`` and ``>``
    (with ``-``/``+`` chomping), quoted and plain scalars, comments.
    Not supported: anchors/aliases, tags, multi-document files, blank lines
    inside block scalars (they are dropped), YAML 1.1 ``yes``/``no`` booleans.
    """
    lines = _prep_lines(text)
    if not lines:
        return None
    value, consumed = _parse_block(lines, 0, lines[0][0])
    if consumed != len(lines):
        indent, content, lineno = lines[consumed]
        raise YamlSubsetError(f"line {lineno}: could not parse {content!r}")
    return value


def _parse_block(lines, i, min_indent):
    if i >= len(lines) or lines[i][0] < min_indent:
        return None, i
    content = lines[i][1]
    if content == "-" or content.startswith("- "):
        return _parse_list(lines, i, lines[i][0])
    return _parse_mapping(lines, i, lines[i][0])


def _split_key(content: str):
    """Split ``key: rest`` at the first colon outside quotes; None if no key."""
    quote = None
    i = 0
    while i < len(content):
        ch = content[i]
        if quote:
            if quote == '"' and ch == "\\":
                i += 1
            elif ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == ":" and (i + 1 == len(content) or content[i + 1] in " \t"):
            key = content[:i].strip()
            if len(key) >= 2 and key[0] == key[-1] and key[0] in "\"'":
                key = key[1:-1]
            return key, content[i + 1:].strip()
        i += 1
    return None, None


def _parse_mapping(lines, i, indent):
    out = {}
    while i < len(lines) and lines[i][0] == indent:
        content = lines[i][1]
        if content == "-" or content.startswith("- "):
            break
        key, rest = _split_key(content)
        if key is None:
            raise YamlSubsetError(f"line {lines[i][2]}: expected 'key: value', got {content!r}")
        i += 1
        if rest == "":
            if i < len(lines) and lines[i][0] > indent:
                out[key], i = _parse_block(lines, i, lines[i][0])
            elif i < len(lines) and lines[i][0] == indent and (
                lines[i][1] == "-" or lines[i][1].startswith("- ")
            ):
                # a list may sit at the same indent as its parent key
                out[key], i = _parse_list(lines, i, indent)
            else:
                out[key] = None
        elif rest in ("|", "|-", "|+", ">", ">-", ">+"):
            out[key], i = _parse_block_scalar(lines, i, indent, rest)
        else:
            out[key] = _parse_inline(rest, lines[i - 1][2])
    if i < len(lines) and lines[i][0] > indent:
        raise YamlSubsetError(f"line {lines[i][2]}: unexpected indentation")
    return out, i


def _parse_list(lines, i, indent):
    out = []
    while i < len(lines) and lines[i][0] == indent and (
        lines[i][1] == "-" or lines[i][1].startswith("- ")
    ):
        item = "" if lines[i][1] == "-" else lines[i][1][2:].strip()
        if not item:
            i += 1
            if i < len(lines) and lines[i][0] > indent:
                val, i = _parse_block(lines, i, lines[i][0])
            else:
                val = None
            out.append(val)
            continue
        key, _rest = _split_key(item)
        if key is not None:
            # a "- key: value" item opens a mapping continued on deeper lines
            sub = [(indent + 2, item, lines[i][2])]
            j = i + 1
            while j < len(lines) and lines[j][0] > indent:
                sub.append(lines[j])
                j += 1
            val, consumed = _parse_mapping(sub, 0, indent + 2)
            if consumed != len(sub):
                raise YamlSubsetError(f"line {sub[consumed][2]}: could not parse list item")
            out.append(val)
            i = j
        else:
            out.append(_parse_inline(item, lines[i][2]))
            i += 1
    return out, i


def _parse_block_scalar(lines, i, key_indent, indicator):
    body = []
    while i < len(lines) and lines[i][0] > key_indent:
        body.append(lines[i][1])
        i += 1
    if indicator[0] == "|":
        text = "\n".join(body)
    else:
        text = " ".join(body)  # folded: single newlines become spaces
    if not indicator.endswith("-"):
        text += "\n"
    return text, i


def _parse_inline(text: str, lineno: int) -> Any:
    s = text.strip()
    if s[:1] in ("{", "["):
        value, pos = _parse_flow(s, 0)
        if s[pos:].strip():
            raise YamlSubsetError(f"line {lineno}: trailing content after flow value")
        return value
    return _parse_scalar(s)


def _parse_scalar(s: str) -> Any:
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        body = s[1:-1]
        if s[0] == '"':
            return body.replace('\\"', '"').replace("\\n", "\n").replace("\\t", "\t").replace("\\\\", "\\")
        return body.replace("''", "'")
    if s in ("true", "True"):
        return True
    if s in ("false", "False"):
        return False
    if s in ("null", "~", ""):
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


def _skip_ws(s: str, pos: int) -> int:
    while pos < len(s) and s[pos] in " \t":
        pos += 1
    return pos


def _flow_token(s: str, pos: int, stop: str):
    quote = None
    start = pos
    while pos < len(s):
        ch = s[pos]
        if quote:
            if quote == '"' and ch == "\\":
                pos += 1
            elif ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch in stop or (ch == ":" and ":" in stop):
            break
        pos += 1
    if quote:
        raise YamlSubsetError("unterminated quoted string in flow value")
    return s[start:pos].strip(), pos


def _flow_value(s: str, pos: int):
    pos = _skip_ws(s, pos)
    if pos < len(s) and s[pos] in "{[":
        return _parse_flow(s, pos)
    token, pos = _flow_token(s, pos, stop=",}]")
    return _parse_scalar(token), pos


def _parse_flow(s: str, pos: int):
    pos = _skip_ws(s, pos)
    if pos >= len(s):
        raise YamlSubsetError("unterminated flow value")
    if s[pos] == "{":
        obj = {}
        pos += 1
        while True:
            pos = _skip_ws(s, pos)
            if pos >= len(s):
                raise YamlSubsetError("unterminated flow mapping")
            if s[pos] == "}":
                return obj, pos + 1
            if s[pos] == ",":
                pos += 1
                continue
            key, pos = _flow_token(s, pos, stop=":")
            pos = _skip_ws(s, pos)
            if pos >= len(s) or s[pos] != ":":
                raise YamlSubsetError(f"expected ':' in flow mapping near {s[pos:pos + 10]!r}")
            key = _parse_scalar(key)
            value, pos = _flow_value(s, pos + 1)
            obj[key] = value
    if s[pos] == "[":
        seq = []
        pos += 1
        while True:
            pos = _skip_ws(s, pos)
            if pos >= len(s):
                raise YamlSubsetError("unterminated flow sequence")
            if s[pos] == "]":
                return seq, pos + 1
            if s[pos] == ",":
                pos += 1
                continue
            value, pos = _flow_value(s, pos)
            seq.append(value)
    raise YamlSubsetError(f"unrecognized flow value near {s[pos:pos + 10]!r}")


# ---------------------------------------------------------------------------
# Repo tree scan
# ---------------------------------------------------------------------------

class RepoTree:
    """The committed files of the repo under verification."""

    def __init__(self, root: Path):
        self.root = root
        self.files = set()
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIPPED_DIRS]
            for name in filenames:
                path = Path(dirpath) / name
                self.files.add(path.relative_to(root).as_posix())

    def exists(self, relpath: str) -> bool:
        return relpath in self.files

    def resolve(self, relpath: str) -> Path:
        return self.root / relpath

    def has_any_file_named(self, filename: str) -> bool:
        return any(Path(f).name == filename for f in self.files)


# ---------------------------------------------------------------------------
# Refusal checks — every check returns None (pass) or a failure reason
# ---------------------------------------------------------------------------

def _check_test_ref(ref: str, tree: RepoTree) -> Optional[str]:
    if "::" in ref:
        file_part, _, nodes = ref.partition("::")
        if not file_part.strip() or not nodes.strip():
            return f"malformed pytest node id: {ref!r}"
        if not tree.exists(file_part):
            return f"test file not found in tree: {file_part}"
        node = nodes.split("::")[-1].strip()
        if node:
            text = tree.resolve(file_part).read_text(encoding="utf-8", errors="replace")
            if f"def {node}" not in text:
                return f"test node not found in {file_part}: {node}"
        return None
    if ":" in ref:
        runner, _, target = ref.partition(":")
        if runner not in TEST_RUNNERS:
            known = ", ".join(sorted(TEST_RUNNERS))
            return f"unknown test runner prefix {runner!r} (known runners: {known})"
        if not target.strip():
            return f"empty test target after {runner!r}: prefix"
        manifest = TEST_RUNNERS[runner]
        if not tree.has_any_file_named(manifest):
            return f"{runner} ref requires {manifest} somewhere in the tree; none found"
        return None
    if not tree.exists(ref):
        return f"test file not found in tree: {ref}"
    return None


def _run_script(script_path: Path, timeout_seconds: float, repo_root: Path) -> Optional[str]:
    if not os.access(script_path, os.R_OK):
        return f"script not found in tree: {script_path.name}"
    if not os.access(script_path, os.X_OK):
        return f"script is not executable (chmod +x): {script_path.name}"
    try:
        proc = subprocess.run(
            [str(script_path)],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return f"script timed out after {timeout_seconds:g}s: {script_path.name}"
    except OSError as exc:
        return f"script could not be executed: {exc}"
    if proc.returncode != 0:
        output = " ".join((proc.stdout or "").split() + (proc.stderr or "").split())
        tail = output[-200:]
        return f"script exited {proc.returncode}: {script_path.name}" + (f" | {tail}" if tail else "")
    return None


def _check_script_ref(entry: dict, ref: str, tree: RepoTree) -> Optional[str]:
    timeout_seconds = entry.get("timeout_seconds", DEFAULT_SCRIPT_TIMEOUT_SECONDS)
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
        return f"timeout_seconds must be a positive number, got {timeout_seconds!r}"
    if not tree.exists(ref):
        return f"script not found in tree: {ref}"
    return _run_script(tree.resolve(ref), float(timeout_seconds), tree.root)


def _walk_dotted(data: Any, segments: list) -> tuple[Optional[Any], Optional[str]]:
    current = data
    for seg in segments:
        if isinstance(current, dict):
            if seg not in current:
                return None, seg
            current = current[seg]
        elif isinstance(current, list):
            if not seg.isdigit() or int(seg) >= len(current):
                return None, seg
            current = current[int(seg)]
        else:
            return None, seg
    return current, None


def _values_equal(pinned: Any, actual: Any) -> bool:
    if isinstance(pinned, bool) != isinstance(actual, bool):
        return False  # True == 1 in Python; refuse the cross-type match
    if isinstance(pinned, dict) and isinstance(actual, dict):
        return pinned.keys() == actual.keys() and all(
            _values_equal(pinned[k], actual[k]) for k in pinned
        )
    if isinstance(pinned, list) and isinstance(actual, list):
        return len(pinned) == len(actual) and all(
            _values_equal(p, a) for p, a in zip(pinned, actual)
        )
    if isinstance(pinned, (int, float)) and isinstance(actual, (int, float)):
        return pinned == actual
    return type(pinned) is type(actual) and pinned == actual


def _check_manifest_field_ref(entry: dict, ref: str, tree: RepoTree) -> Optional[str]:
    file_part, sep, dotted = ref.partition(":")
    if not sep or not file_part.strip() or not dotted.strip():
        return "manifest_field ref must be '<file>:<dotted.path>'"
    if not tree.exists(file_part):
        return f"manifest file not found in tree: {file_part}"
    path = tree.resolve(file_part)
    ext = path.suffix.lower()
    try:
        if ext == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
        elif ext in (".yaml", ".yml"):
            data = load_yaml(path.read_text(encoding="utf-8"))
        else:
            return f"unsupported manifest format {ext!r} (use .json, .yaml, or .yml)"
    except (json.JSONDecodeError, YamlSubsetError, UnicodeDecodeError, OSError) as exc:
        return f"manifest parse error in {file_part}: {exc}"
    segments = dotted.split(".")
    value, missing = _walk_dotted(data, segments)
    if missing is not None:
        return f"dotted path not found in {file_part}: {'.'.join(segments)} (at {missing!r})"
    pinned = entry.get("equals")
    if "equals" not in entry:
        return "manifest_field evidence requires an 'equals' pinned value"
    if not _values_equal(pinned, value):
        return f"manifest field mismatch at {dotted}: pinned {pinned!r}, found {value!r}"
    return None


def _check_artifact_hash_ref(entry: dict, ref: str, tree: RepoTree) -> Optional[str]:
    pin = entry.get("sha256")
    if "sha256" not in entry or not isinstance(pin, str):
        return "artifact_hash evidence requires a 'sha256' pin"
    pin = pin.strip().lower()
    if len(pin) != 64 or any(c not in "0123456789abcdef" for c in pin):
        return f"malformed sha256 pin (expected 64 hex chars): {entry.get('sha256')!r}"
    if not tree.exists(ref):
        return f"artifact not found in tree: {ref}"
    digest = hashlib.sha256()
    with tree.resolve(ref).open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    computed = digest.hexdigest()
    if computed != pin:
        return f"artifact hash mismatch for {ref}: pinned {pin[:12]}…, computed {computed[:12]}…"
    return None


def _check_evidence_entry(entry: Any, tree: RepoTree) -> tuple[str, Optional[str]]:
    """Return (ref_label, failure_reason_or_None) for one evidence entry."""
    if not isinstance(entry, dict):
        return str(entry)[:60], "evidence entry is not a mapping"
    etype = entry.get("type")
    if etype not in EVIDENCE_TYPES:
        allowed = ", ".join(EVIDENCE_TYPES)
        return str(etype)[:60], f"unknown evidence type {etype!r} (allowed: {allowed})"
    unknown = sorted(set(entry) - EVIDENCE_KEYS[etype])
    if unknown:
        return "?", f"unknown field(s) in {etype} evidence entry: {', '.join(unknown)}"
    ref = entry.get("ref")
    if not isinstance(ref, str) or not ref.strip():
        return "?", f"{etype} evidence ref is missing or empty"
    ref = ref.strip()
    if etype == "test":
        return ref, _check_test_ref(ref, tree)
    if etype == "script":
        return ref, _check_script_ref(entry, ref, tree)
    if etype == "manifest_field":
        return ref, _check_manifest_field_ref(entry, ref, tree)
    return ref, _check_artifact_hash_ref(entry, ref, tree)


# ---------------------------------------------------------------------------
# Claim-level checks
# ---------------------------------------------------------------------------

@dataclass
class RefVerdict:
    claim_id: str
    claim_text: str
    ref: str
    reason: Optional[str]  # None = pass

    @property
    def ok(self) -> bool:
        return self.reason is None


def _is_cprefixed_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) > 1
        and value[0] == "C"
        and value[1:].isdigit()
    )


def verify_claim(claim: Any, index: int, tree: RepoTree, seen_ids: dict) -> list:
    """Verify one claim row; returns its RefVerdicts plus inline global failures."""
    outcomes = []
    if not isinstance(claim, dict):
        return outcomes  # recorded as a global failure by the caller

    cid = claim.get("id", f"<claim {index}>")
    text = claim.get("claim", "")

    unknown = sorted(set(claim) - CLAIM_KEYS)
    if unknown:
        outcomes.append(RefVerdict(str(cid), str(text), "<schema>",
                                   f"unknown field(s) in claim: {', '.join(unknown)}"))
    if not _is_cprefixed_id(claim.get("id")):
        outcomes.append(RefVerdict(str(cid), str(text), "<schema>",
                                   f"claim id must be a unique C-prefixed id (C001, C002, …), got {claim.get('id')!r}"))
    elif claim["id"] in seen_ids:
        outcomes.append(RefVerdict(str(cid), str(text), "<schema>",
                                   f"duplicate claim id: {claim['id']} (first used by claim index {seen_ids[claim['id']]})"))
    else:
        seen_ids[claim["id"]] = index
    if not isinstance(claim.get("claim"), str) or not claim["claim"].strip():
        outcomes.append(RefVerdict(str(cid), str(text), "<schema>",
                                   "claim text is missing or empty"))

    source = claim.get("source")
    if not isinstance(source, str) or not source.strip():
        outcomes.append(RefVerdict(str(cid), str(text), "<source>",
                                   "claim has no source locator"))
    else:
        source_file = source.strip().partition("#")[0].strip()
        if not source_file:
            outcomes.append(RefVerdict(str(cid), str(text), source.strip(),
                                       "source locator names no file"))
        elif not tree.exists(source_file):
            outcomes.append(RefVerdict(str(cid), str(text), source.strip(),
                                       f"source locator points at a file that does not exist: {source_file}"))

    evidence = claim.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        outcomes.append(RefVerdict(str(cid), str(text), "<evidence>",
                                   "zero-evidence row: every claim needs at least one evidence ref"))
        return outcomes

    for entry in evidence:
        ref, reason = _check_evidence_entry(entry, tree)
        outcomes.append(RefVerdict(str(cid), str(text), ref, reason))
    return outcomes


# ---------------------------------------------------------------------------
# Matrix-level verification
# ---------------------------------------------------------------------------

def default_repo_root() -> Path:
    here = Path(__file__).resolve()
    if here.parent.name == "tools":
        return here.parent.parent
    return Path.cwd()


def load_matrix(matrix_path: Path) -> Any:
    if not matrix_path.is_file():
        raise UnusableMatrix(
            f"no evidence matrix found at {matrix_path} — this repo's capability "
            "claims are not machine-verifiable"
        )
    try:
        text = matrix_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise UnusableMatrix(f"could not read {matrix_path}: {exc}")
    try:
        return load_yaml(text)
    except YamlSubsetError as exc:
        raise UnusableMatrix(f"malformed YAML in {matrix_path}: {exc}")
    except Exception as exc:  # PyYAML's parse errors
        raise UnusableMatrix(f"malformed YAML in {matrix_path}: {exc}")


def _claim_text_line(text: Any, width: int = 64) -> str:
    single = " ".join(str(text).split())
    return single if len(single) <= width else single[: width - 1] + "…"


def verify_matrix(doc: Any, matrix_path: Path) -> tuple[list, list, int]:
    """Verify a parsed matrix. Returns (verdicts, global_failures, n_claims)."""
    if not isinstance(doc, dict):
        raise UnusableMatrix(f"{matrix_path}: matrix root must be a mapping")

    schema_version = doc.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise UnusableMatrix(
            f"{matrix_path}: schema_version must be the integer {SCHEMA_VERSION}, "
            f"got {schema_version!r}"
        )
    if schema_version != SCHEMA_VERSION:
        raise UnusableMatrix(
            f"{matrix_path}: unsupported schema_version {schema_version} "
            f"(this verifier implements schema v{SCHEMA_VERSION} — a major bump is a "
            "breaking change requiring kit-level review)"
        )

    verdicts = []
    global_failures = []

    unknown_top = sorted(set(doc) - TOP_LEVEL_KEYS)
    if unknown_top:
        allowed = ", ".join(sorted(TOP_LEVEL_KEYS))
        global_failures.append(f"unknown top-level field(s): {', '.join(unknown_top)} (schema v1 allows: {allowed})")

    repo_name = doc.get("repo")
    if repo_name is not None and (not isinstance(repo_name, str) or not repo_name.strip()):
        global_failures.append("top-level 'repo' must be a non-empty string when present")

    claims = doc.get("claims")
    if claims is None or not isinstance(claims, list):
        global_failures.append(f"'claims' must be a list, got {type(claims).__name__}")
        return verdicts, global_failures, 0
    if not claims:
        global_failures.append(
            "matrix declares no claims — a matrix that verifies nothing is not decision-ready"
        )
        return verdicts, global_failures, 0

    tree = RepoTree(tree_root(matrix_path))
    seen_ids = {}
    for index, claim in enumerate(claims):
        if not isinstance(claim, dict):
            global_failures.append(f"claim at index {index} is not a mapping")
            continue
        verdicts.extend(verify_claim(claim, index, tree, seen_ids))
    return verdicts, global_failures, len(claims)


def tree_root(matrix_path: Path) -> Path:
    """The repo root the refs in this matrix resolve against."""
    return matrix_path.parent.parent.resolve()


def print_report(verdicts, global_failures, n_claims, matrix_path) -> int:
    """Print the verdict table and closing line; returns the failing-claim count."""
    print(f"=== evidence matrix: {matrix_path} ===")
    print(f"verifier: verify_evidence_matrix.py v{EVIDENCE_MATRIX_VERIFIER_VERSION} "
          "(canonical kit: icohangar-ops/consensus-hardening-protocol)")

    failed_claim_ids = set()
    current_id = None
    for v in verdicts:
        if v.claim_id != current_id:
            current_id = v.claim_id
            print(f"{v.claim_id}  claim: {_claim_text_line(v.claim_text)}")
        if v.ok:
            print(f"    {v.ref}  ->  PASS")
        else:
            print(f"    {v.ref}  ->  FAIL  —  {v.reason}")
            failed_claim_ids.add(v.claim_id)

    ref_failures = [v for v in verdicts if not v.ok]
    all_failures = global_failures + [f"{v.claim_id} {v.ref} — {v.reason}" for v in ref_failures]
    if all_failures:
        print(f"\nfailures ({len(all_failures)}):")
        for failure in all_failures:
            print(f"  - {failure}")

    # k counts unverified CLAIMS; global (matrix-level) failures are listed in the
    # failures block and force FAILED without inflating the claim count.
    k = len(failed_claim_ids)
    if not all_failures and n_claims > 0 and k == 0:
        print(f"\nEVIDENCE MATRIX: VERIFIED ({n_claims}/{n_claims})")
        return EXIT_OK
    print(f"\nEVIDENCE MATRIX: FAILED ({k} of {n_claims} unverified)")
    return EXIT_FAILED


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail-closed verifier for evidence/matrix.yaml (schema v1). "
                    "No skip flags, no allowlists, no quiet modes."
    )
    parser.add_argument("--matrix", default=None,
                        help="path to the matrix file (default: <repo-root>/evidence/matrix.yaml)")
    parser.add_argument("--repo-root", default=None,
                        help="repo root that evidence refs resolve against "
                             "(default: the directory containing the tools/ dir this file sits in)")
    parser.add_argument("--version", action="version",
                        version=f"verify_evidence_matrix {EVIDENCE_MATRIX_VERIFIER_VERSION}")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve() if args.repo_root else default_repo_root()
    matrix_path = Path(args.matrix) if args.matrix else repo_root.joinpath(*DEFAULT_MATRIX_RELPATH)

    try:
        doc = load_matrix(matrix_path)
        verdicts, global_failures, n_claims = verify_matrix(doc, matrix_path)
    except UnusableMatrix as exc:
        print(f"EVIDENCE MATRIX: UNUSABLE — {exc}", file=sys.stderr)
        return EXIT_UNUSABLE

    return print_report(verdicts, global_failures, n_claims, matrix_path)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
