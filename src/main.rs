//! cohortc — a deterministic cohort compiler.
//!
//! Compiles a tiny query language into dialect-specific SQL, resolving every population
//! reference against a semantic registry. No LLM, no inference, no defaults: the same
//! input produces byte-identical SQL every time.
//!
//! WHY THIS EXISTS
//!
//! Asking "how many psychiatrists?" against one warehouse returns 51,964 / 41,823 / 22,956
//! depending on which of four plausible columns you believe. Narrowing the same question to
//! one manufacturer's payees returns 572 or 2,798 — a 4.9x spread. Every answer is
//! defensible and none is labelled,
//! so two analysts produce different numbers for the same question and neither is wrong.
//!
//! Text-to-SQL does not fix this. It makes it worse: a model silently picks one definition
//! and states the result with confidence. The ambiguity is in the SEMANTICS, not the syntax,
//! so a better query language (PRQL) or a dialect transpiler does not address it either.
//!
//! cohortc refuses to compile an ambiguous reference. `psychiatrists` is an error that lists
//! the three definitions; `psychiatrists@xcloud` compiles. The choice is forced to be
//! explicit and it is recorded in the emitted SQL.

use serde::Deserialize;
use std::collections::BTreeMap;
use std::fmt::Write as _;

#[derive(Debug, Deserialize)]
struct Registry {
    dialects: Vec<String>,
    entities: BTreeMap<String, Entity>,
    cohorts: BTreeMap<String, Cohort>,
    measures: BTreeMap<String, Metric>,
}

#[derive(Debug, Deserialize)]
struct Entity {
    table: String,
    #[allow(dead_code)]
    key: String,
}

#[derive(Debug, Deserialize)]
struct Cohort {
    entity: String,
    #[serde(default)]
    ambiguous: bool,
    definitions: BTreeMap<String, Definition>,
}

#[derive(Debug, Deserialize)]
struct Definition {
    description: String,
    predicate: String,
    #[serde(default)]
    measured: Option<u64>,
}

#[derive(Debug, Deserialize)]
struct Metric {
    #[serde(default)]
    ambiguous: bool,
    definitions: BTreeMap<String, MetricDefinition>,
}

#[derive(Debug, Deserialize)]
struct MetricDefinition {
    description: String,
    expression: String,
    unit: String,
}

#[derive(Debug, PartialEq)]
struct Query {
    cohort: String,
    variant: Option<String>,
    filters: Vec<String>,
    measures: Vec<String>,
    group_by: Vec<String>,
    limit: Option<u32>,
}

#[derive(Debug)]
enum CompileError {
    UnknownCohort {
        name: String,
        known: Vec<String>,
    },
    /// The whole point of the tool.
    AmbiguousCohort {
        name: String,
        options: Vec<(String, String, Option<u64>)>,
    },
    UnknownVariant {
        cohort: String,
        variant: String,
        known: Vec<String>,
    },
    UnknownMeasure {
        name: String,
        known: Vec<String>,
    },
    AmbiguousMeasure {
        name: String,
        options: Vec<(String, String, String)>,
    },
    UnknownDialect {
        name: String,
        known: Vec<String>,
    },
    Parse(String),
}

impl std::fmt::Display for CompileError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            CompileError::UnknownCohort { name, known } => write!(
                f,
                "unknown cohort `{name}`\n  known cohorts: {}",
                known.join(", ")
            ),
            CompileError::AmbiguousCohort { name, options } => {
                writeln!(f, "`{name}` is ambiguous and has no default.")?;
                writeln!(
                    f,
                    "\n  It has {} definitions that return different answers:\n",
                    options.len()
                )?;
                for (variant, desc, measured) in options {
                    let n = measured
                        .map(|m| format!("{:>9}", fmt_thousands(m)))
                        .unwrap_or_else(|| "        ?".into());
                    writeln!(f, "    {n}   {name}@{variant}")?;
                    writeln!(f, "                {desc}")?;
                }
                write!(
                    f,
                    "\n  Name one explicitly, e.g. `{name}@{}`.\n  \
                     Refusing to guess: the definitions differ by more than 2x.",
                    options[0].0
                )
            }
            CompileError::UnknownVariant {
                cohort,
                variant,
                known,
            } => write!(
                f,
                "`{cohort}` has no definition `{variant}`\n  known: {}",
                known.join(", ")
            ),
            CompileError::UnknownMeasure { name, known } => write!(
                f,
                "unknown measure `{name}`\n  known measures: {}",
                known.join(", ")
            ),
            CompileError::AmbiguousMeasure { name, options } => {
                writeln!(f, "`{name}` is ambiguous and has no default.")?;
                writeln!(f, "\n  Name one metric definition explicitly:\n")?;
                for (variant, description, unit) in options {
                    writeln!(f, "    {name}@{variant} [{unit}]")?;
                    writeln!(f, "                {description}")?;
                }
                write!(
                    f,
                    "\n  Refusing to guess: the metric contract requires a named definition."
                )
            }
            CompileError::UnknownDialect { name, known } => {
                write!(f, "unknown dialect `{name}`\n  known: {}", known.join(", "))
            }
            CompileError::Parse(m) => write!(f, "parse error: {m}"),
        }
    }
}

fn fmt_thousands(n: u64) -> String {
    let s = n.to_string();
    let mut out = String::new();
    for (i, c) in s.chars().enumerate() {
        if i > 0 && (s.len() - i) % 3 == 0 {
            out.push(',');
        }
        out.push(c);
    }
    out
}

/// Deliberately minimal and line-oriented. A bigger grammar is not the problem being solved.
fn parse(src: &str) -> Result<Query, CompileError> {
    let mut q = Query {
        cohort: String::new(),
        variant: None,
        filters: vec![],
        measures: vec![],
        group_by: vec![],
        limit: None,
    };
    for raw in src.lines() {
        let line = raw.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let (kw, rest) = line.split_once(char::is_whitespace).unwrap_or((line, ""));
        let rest = rest.trim();
        match kw {
            "cohort" => {
                let (c, v) = match rest.split_once('@') {
                    Some((c, v)) => (c.trim().to_string(), Some(v.trim().to_string())),
                    None => (rest.to_string(), None),
                };
                q.cohort = c;
                q.variant = v;
            }
            "and" | "where" => q.filters.push(rest.to_string()),
            "measure" => q.measures.extend(
                rest.split(',')
                    .map(|s| s.trim().to_string())
                    .filter(|s| !s.is_empty()),
            ),
            "by" => q.group_by.extend(
                rest.split(',')
                    .map(|s| s.trim().to_string())
                    .filter(|s| !s.is_empty()),
            ),
            "limit" => {
                q.limit = Some(rest.parse().map_err(|_| {
                    CompileError::Parse(format!("`limit` expects a number, got `{rest}`"))
                })?)
            }
            other => {
                return Err(CompileError::Parse(format!(
                "unexpected keyword `{other}` (expected: cohort, where, and, measure, by, limit)"
            )))
            }
        }
    }
    if q.cohort.is_empty() {
        return Err(CompileError::Parse("no `cohort` line".into()));
    }
    if q.measures.is_empty() {
        q.measures.push("count".into());
    }
    Ok(q)
}

fn compile(reg: &Registry, q: &Query, dialect: &str) -> Result<String, CompileError> {
    if !reg.dialects.iter().any(|d| d == dialect) {
        return Err(CompileError::UnknownDialect {
            name: dialect.into(),
            known: reg.dialects.clone(),
        });
    }
    let cohort = reg
        .cohorts
        .get(&q.cohort)
        .ok_or_else(|| CompileError::UnknownCohort {
            name: q.cohort.clone(),
            known: reg.cohorts.keys().cloned().collect(),
        })?;

    // The refusal. A cohort marked ambiguous, referenced without a variant, does not compile.
    let variant = match &q.variant {
        Some(v) => v.clone(),
        None => {
            if cohort.ambiguous || cohort.definitions.len() > 1 {
                let mut options: Vec<(String, String, Option<u64>)> = cohort
                    .definitions
                    .iter()
                    .map(|(k, d)| (k.clone(), d.description.clone(), d.measured))
                    .collect();
                options.sort_by(|a, b| b.2.cmp(&a.2));
                return Err(CompileError::AmbiguousCohort {
                    name: q.cohort.clone(),
                    options,
                });
            }
            "default".to_string()
        }
    };
    let def = cohort
        .definitions
        .get(&variant)
        .ok_or_else(|| CompileError::UnknownVariant {
            cohort: q.cohort.clone(),
            variant: variant.clone(),
            known: cohort.definitions.keys().cloned().collect(),
        })?;
    let entity = reg
        .entities
        .get(&cohort.entity)
        .ok_or_else(|| CompileError::UnknownCohort {
            name: cohort.entity.clone(),
            known: reg.entities.keys().cloned().collect(),
        })?;

    let mut selects: Vec<String> = q.group_by.clone();
    for m in &q.measures {
        let (name, requested_variant) = match m.split_once('@') {
            Some((name, variant)) => (name, Some(variant)),
            None => (m.as_str(), None),
        };
        let metric = reg
            .measures
            .get(name)
            .ok_or_else(|| CompileError::UnknownMeasure {
                name: m.clone(),
                known: reg.measures.keys().cloned().collect(),
            })?;
        let variant = match requested_variant {
            Some(v) => v,
            None => {
                if metric.ambiguous || metric.definitions.len() > 1 {
                    let mut options: Vec<(String, String, String)> = metric
                        .definitions
                        .iter()
                        .map(|(k, d)| (k.clone(), d.description.clone(), d.unit.clone()))
                        .collect();
                    options.sort_by(|a, b| a.0.cmp(&b.0));
                    return Err(CompileError::AmbiguousMeasure {
                        name: name.into(),
                        options,
                    });
                }
                "default"
            }
        };
        let definition =
            metric
                .definitions
                .get(variant)
                .ok_or_else(|| CompileError::UnknownMeasure {
                    name: m.clone(),
                    known: reg.measures.keys().cloned().collect(),
                })?;
        let alias = m.replace('@', "_");
        selects.push(format!("{} as {alias}", definition.expression));
    }

    let mut preds = vec![def.predicate.clone()];
    preds.extend(q.filters.clone());

    // Provenance in the SQL itself, so a query pasted into a deck carries its definition.
    let mut sql = String::new();
    writeln!(sql, "-- cohortc: {}@{}", q.cohort, variant).unwrap();
    writeln!(sql, "-- {}", def.description).unwrap();
    if let Some(m) = def.measured {
        writeln!(sql, "-- registry-recorded size: {}", fmt_thousands(m)).unwrap();
    }
    writeln!(sql, "-- dialect: {dialect}").unwrap();
    writeln!(sql, "select").unwrap();
    writeln!(sql, "  {}", selects.join(",\n  ")).unwrap();
    writeln!(sql, "from {}", entity.table).unwrap();
    writeln!(sql, "where {}", preds.join("\n  and ")).unwrap();
    if !q.group_by.is_empty() {
        let keys: Vec<String> = (1..=q.group_by.len()).map(|i| i.to_string()).collect();
        writeln!(sql, "group by {}", keys.join(", ")).unwrap();
        // Deterministic ordering. ClickHouse and DuckDB both accept positional ORDER BY,
        // but an unordered result is not reproducible, which defeats the purpose.
        writeln!(sql, "order by {} desc", q.group_by.len() + 1).unwrap();
    }
    if let Some(n) = q.limit {
        writeln!(sql, "limit {n}").unwrap();
    }
    Ok(sql)
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let mut dialect = "duckdb".to_string();
    let mut path: Option<String> = None;
    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--dialect" => {
                i += 1;
                dialect = args.get(i).cloned().unwrap_or_else(|| "duckdb".into());
            }
            "--registry" => {
                i += 1;
                path = args.get(i).cloned();
            }
            _ => {}
        }
        i += 1;
    }
    let reg_path = path.unwrap_or_else(|| "registry/cohorts.yaml".into());
    let reg_src = match std::fs::read_to_string(&reg_path) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("cannot read registry {reg_path}: {e}");
            std::process::exit(2);
        }
    };
    let reg: Registry = match serde_yaml::from_str(&reg_src) {
        Ok(r) => r,
        Err(e) => {
            eprintln!("registry is not valid: {e}");
            std::process::exit(2);
        }
    };
    let mut src = String::new();
    use std::io::Read;
    if std::io::stdin().read_to_string(&mut src).is_err() {
        eprintln!("cannot read query from stdin");
        std::process::exit(2);
    }
    match parse(&src).and_then(|q| compile(&reg, &q, &dialect)) {
        Ok(sql) => print!("{sql}"),
        Err(e) => {
            eprintln!("error: {e}");
            std::process::exit(1);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn reg() -> Registry {
        serde_yaml::from_str(&std::fs::read_to_string("registry/cohorts.yaml").unwrap()).unwrap()
    }

    #[test]
    fn ambiguous_cohort_refuses_to_compile() {
        let q = parse("cohort psychiatrists\nmeasure count").unwrap();
        let err = compile(&reg(), &q, "duckdb").unwrap_err();
        match err {
            CompileError::AmbiguousCohort { name, options } => {
                assert_eq!(name, "psychiatrists");
                assert_eq!(options.len(), 3, "all three definitions must be offered");
            }
            other => panic!("expected AmbiguousCohort, got {other:?}"),
        }
    }

    #[test]
    fn qualified_cohort_compiles() {
        let q = parse("cohort psychiatrists@xcloud\nmeasure count").unwrap();
        let sql = compile(&reg(), &q, "duckdb").unwrap();
        assert!(sql.contains("x_cloud_abbrev = 'PSY'"));
        assert!(sql.contains("-- cohortc: psychiatrists@xcloud"));
    }

    /// Determinism is the product claim, so it is tested rather than asserted.
    #[test]
    fn compilation_is_byte_identical_across_runs() {
        let src =
            "cohort psychiatrists@nucc\nwhere state = 'CA'\nmeasure count, industry_usd\nby state";
        let r = reg();
        let a = compile(&r, &parse(src).unwrap(), "duckdb").unwrap();
        for _ in 0..50 {
            assert_eq!(a, compile(&r, &parse(src).unwrap(), "duckdb").unwrap());
        }
    }

    #[test]
    fn unqualified_single_definition_cohort_is_fine() {
        let q = parse("cohort untapped_bench\nmeasure clinicians\nby state").unwrap();
        let sql = compile(&reg(), &q, "duckdb").unwrap();
        assert!(sql.contains("not is_named_investigator"));
        assert!(sql.contains("group by 1"));
    }

    #[test]
    fn unknown_measure_is_rejected_with_the_known_list() {
        let q = parse("cohort kol\nmeasure revenue").unwrap();
        match compile(&reg(), &q, "duckdb").unwrap_err() {
            CompileError::UnknownMeasure { name, known } => {
                assert_eq!(name, "revenue");
                assert!(known.contains(&"industry_usd".to_string()));
            }
            other => panic!("expected UnknownMeasure, got {other:?}"),
        }
    }

    #[test]
    fn ambiguous_metric_requires_a_named_definition() {
        let q = parse("cohort kol\nmeasure spend").unwrap();
        match compile(&reg(), &q, "duckdb").unwrap_err() {
            CompileError::AmbiguousMeasure { name, options } => {
                assert_eq!(name, "spend");
                assert!(options.iter().any(|(variant, _, _)| variant == "cash_paid"));
                assert!(options.iter().any(|(variant, _, _)| variant == "invoiced"));
            }
            other => panic!("expected AmbiguousMeasure, got {other:?}"),
        }
    }

    #[test]
    fn qualified_procurement_metric_uses_a_stable_alias() {
        let q = parse("cohort kol\nmeasure gross_margin@revenue_percent").unwrap();
        let sql = compile(&reg(), &q, "duckdb").unwrap();
        assert!(sql.contains("as gross_margin_revenue_percent"));
    }

    #[test]
    fn dialects_are_validated_against_the_registry() {
        let q = parse("cohort kol\nmeasure count").unwrap();
        assert!(compile(&reg(), &q, "snowflake").is_err());
        assert!(compile(&reg(), &q, "clickhouse").is_ok());
    }

    /// The README promises a deterministic `order by` on every group-by; pin it.
    #[test]
    fn group_by_emits_deterministic_order_by() {
        let q = parse("cohort kol\nmeasure clinicians\nby state").unwrap();
        let sql = compile(&reg(), &q, "duckdb").unwrap();
        assert!(sql.contains("group by 1"));
        assert!(sql.contains("order by 2 desc"));
    }

    /// Honest limitation, pinned rather than asserted: the dialect is validated and
    /// recorded in the provenance header, while the SQL itself is byte-identical across
    /// DuckDB and ClickHouse today. When divergence handling lands, this test fails and
    /// forces the README limitation to change with it.
    #[test]
    fn duckdb_and_clickhouse_emit_identical_sql_today() {
        let r = reg();
        let src = "cohort kol\nmeasure count, clinicians\nby state";
        let duck = compile(&r, &parse(src).unwrap(), "duckdb").unwrap();
        let click = compile(&r, &parse(src).unwrap(), "clickhouse").unwrap();
        assert!(duck.contains("-- dialect: duckdb"));
        assert!(click.contains("-- dialect: clickhouse"));
        let duck_sql = duck.split("-- dialect: duckdb\n").nth(1).unwrap();
        let click_sql = click.split("-- dialect: clickhouse\n").nth(1).unwrap();
        assert_eq!(
            duck_sql, click_sql,
            "SQL diverged by dialect; update the README limitation when this lands"
        );
    }
}
