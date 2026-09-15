# cohortc

**A deterministic cohort compiler. It refuses to answer ambiguous questions.**

Pure Rust, two dependencies, no LLM.

---

## The problem

Ask your warehouse a simple question:

> How many psychiatrists are in the data?

Here are three answers from the same table, on the same day:

| Answer | Predicate |
|---:|---|
| **51,964** | `x_cloud_abbrev = 'PSY'` — the curated segment assignment |
| **41,823** | `lower(specialty_raw_nucc) like '%psychiatry%'` — the provider registry's taxonomy |
| **22,956** | `lower(subspecialty) like '%psychiatry%'` — declared subspecialty only |

**A 2.3x spread. All three are defensible. None is labelled.**

Narrow the same question to one manufacturer's payees and it returns **572 or 2,798** — a
4.9x spread — depending entirely on which of those columns the analyst reached for.

This is not a hypothetical. Both of those numbers were published, in separate documents, by
the same person, a day apart. Neither was wrong. Nobody noticed, because **each individual
answer looks correct in isolation.** There is no error message. There is no failing test.
There is just a number in a deck that cannot be traced back to what it counted.

### Why this is a naming problem, not a query problem

Three things it is tempting to reach for, none of which help:

| | Why it does not fix this |
|---|---|
| **A better query language** (PRQL, Malloy) | Improves the *syntax* of expressing a query. The three predicates above are all easy to write in any syntax. |
| **A dialect transpiler** (sqlglot, Polyglot) | Translates between SQL flavours. The ambiguity exists identically in every dialect. |
| **Text-to-SQL** (any NL→SQL agent) | **Makes it worse.** A model asked "how many psychiatrists?" picks one of the three silently and states the result with confidence. The one tool that could have surfaced the ambiguity instead conceals it. |

The ambiguity lives in the gap between a business noun and a column predicate. That gap is
filled ad hoc, by whoever writes the query, and then forgotten.

---

## What cohortc does

It makes the definition a **named artifact**, and refuses to compile a reference that does
not pick one.

```
$ echo 'cohort psychiatrists
        measure count' | cohortc

error: `psychiatrists` is ambiguous and has no default.

  It has 3 definitions that return different answers:

       51,964   psychiatrists@xcloud
                Curated segment assignment. Widest; what downstream activation runs on.
       41,823   psychiatrists@nucc
                NUCC primary taxonomy text. Broader; what the provider registry says.
       22,956   psychiatrists@subspecialty
                Declared subspecialty only. Narrowest; excludes generalists.

  Name one explicitly, e.g. `psychiatrists@xcloud`.
  Refusing to guess: the definitions differ by more than 2x.
```

Qualify it and you get SQL that **carries its own provenance**, so a query pasted into a deck
arrives with its definition attached:

```
$ echo 'cohort psychiatrists@xcloud
        where state = '"'"'CA'"'"'
        measure count, industry_usd
        by state' | cohortc

-- cohortc: psychiatrists@xcloud
-- Curated segment assignment. Widest; what downstream activation runs on.
-- registry-recorded size: 51,964
-- dialect: duckdb
select
  state,
  count(*) as count,
  round(sum(op_total_dollars)) as industry_usd
from mart_marketing_audience
where x_cloud_abbrev = 'PSY'
  and state = 'CA'
group by 1
order by 2 desc
```

## The registry

One YAML file. Every countable population is named here, once, with an explicit predicate.
Nothing else may define a population.

```yaml
cohorts:
  psychiatrists:
    entity: clinician
    ambiguous: true          # a bare reference refuses to compile
    definitions:
      xcloud:
        description: Curated segment assignment. Widest; what downstream activation runs on.
        predicate: "x_cloud_abbrev = 'PSY'"
        measured: 51964      # checked against the warehouse; drift is visible

measures:
  count:        "count(*)"
  industry_usd: "round(sum(op_total_dollars))"
```

`measured` is the size recorded when the definition was written. Re-running the compiled SQL
and comparing is a one-line CI check — when a definition silently changes meaning, you find
out from a failing build rather than from a contradiction in a meeting.

## Determinism

The product claim is that the same input yields byte-identical SQL, so it is tested rather
than asserted:

```rust
#[test]
fn compilation_is_byte_identical_across_runs() { /* compiles the same query 50x */ }
```

Measures are a **closed vocabulary** — an unknown one is rejected with the valid list, not
silently passed through to the database. Group-by always emits a deterministic `order by`,
because an unordered result is not reproducible.

## Using it with an LLM

cohortc does not replace a natural-language interface. It makes one safe to use, by splitting
the work at the right seam:

- The **model** picks a cohort *name* from a closed vocabulary — a constrained choice it is good at.
- The **compiler** generates the SQL — deterministic, testable, auditable.

An agent that emits `psychiatrists@nucc` has made its assumption legible and reviewable. An
agent that emits raw SQL has buried it.

## Build

```bash
cargo build --release
cargo test                              # 6 tests
echo 'cohort kol
      measure clinicians
      by state' | ./target/release/cohortc --dialect clickhouse
```

## What this deliberately is not

- **Not a query engine.** It emits SQL; your warehouse runs it.
- **Not a full query language.** Six keywords: `cohort`, `where`, `and`, `measure`, `by`, `limit`. If you need joins and pipelines, the emitted SQL is a starting point, or wrap a real language around the registry.
- **Not a dbt replacement.** dbt builds the tables. cohortc names the populations inside them.
- **Not text-to-SQL.** That is the failure mode it exists to prevent.

## Honest limitations

**SQL generation is string concatenation.** There is no SQL parser here, so predicates in the
registry are trusted verbatim and never validated. A registry is a trusted artifact, like a
dbt model — but it does mean a typo surfaces as a database error, not a compile error.
`sqlparser-rs` is the right dependency to add when that starts to hurt.

**Dialect support is currently nominal.** `--dialect` is validated against the registry and
recorded in the output, but the DuckDB and ClickHouse SQL are byte-identical today. The seam
exists; the divergence handling does not.

**The grammar will run out.** Six keywords covers single-table aggregates. Joins, window
functions and nested filters are not expressible and are not planned — the point of the tool
is the registry, not the language.

## Licence

MIT.
