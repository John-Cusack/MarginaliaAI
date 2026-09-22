//! Extraction schema parsing, prompt rendering, and the LLM output contract.
//!
//! Python source:
//! `packages/core/src/research_engine/services/extraction/schemas.py`.
//!
//! An extraction schema is authored as YAML: a list of `record_types`, each
//! with named `fields`, plus a Jinja2 `prompt`. Three things have to agree
//! about the shape of what comes back from the model — the JSON Schema sent
//! to the provider, the validator, and the `extraction_records` table — and
//! this module is where that shape is defined once so they cannot drift.
//!
//! The contract is deliberately flat:
//! `{"records": [{"record_type": "claim", "fields": {...}}]}`.
//! Nesting record types as keys reads more naturally, but storage needs
//! `extraction_records.record_type` as a column, so each record names its own
//! type.

use serde_json::{Map, Value};

use crate::errors::{Error, Result};

/// Field type meaning "a verbatim quotation from the passage". Every record
/// type must declare at least one: it is what makes an extracted claim
/// checkable against the corpus rather than merely plausible.
pub const EVIDENCE_TYPE: &str = "evidence_span";

/// Authored field type -> JSON Schema base type.
///
/// Types absent here used to be passed through verbatim, so a field declared
/// `type: date` produced `{"type": "date"}` — not a JSON Schema type, and
/// rejected outright by a provider that validates. Anything unlisted is
/// carried as a string.
fn json_base_type(declared: &str) -> &'static str {
    match declared {
        "string" | "text" | "entity_ref" | "fuzzy_date" | "enum" => "string",
        EVIDENCE_TYPE => "string",
        "number" => "number",
        "integer" => "integer",
        "boolean" => "boolean",
        "array" => "array",
        _ => "string",
    }
}

/// Parse an extraction schema YAML string.
///
/// `yaml.safe_load` of an empty document answers `None`, while `serde_yaml`
/// fails the same input — so blank input maps to `Null` here, keeping the
/// "empty schema text parses" behavior without inventing a mapping.
pub fn parse_schema_yaml(content: &str) -> Result<Value> {
    if content.trim().is_empty() {
        return Ok(Value::Null);
    }
    serde_yaml::from_str(content).map_err(|err| Error::Validation(err.to_string()))
}

/// Render an extraction prompt template.
///
/// Minimal `{{ name }}` renderer over `passage_text`, `entity_hints`
/// (default `""`), and a flattened `extra_context` (which wins on key
/// collision, as in the Python `{**base, **extra}` merge). A name with no
/// binding fails like Jinja2's `StrictUndefined` — a `Validation` error
/// naming the variable.
///
/// Scope: authored prompts only interpolate names. Jinja2 tags, filters,
/// tests, and comments stay Python-side; anything that is not a `{{ name }}`
/// placeholder passes through verbatim.
pub fn render_prompt(
    template: &str,
    passage_text: &str,
    entity_hints: &str,
    extra_context: Option<&Map<String, Value>>,
) -> Result<String> {
    let mut rendered = String::with_capacity(template.len() + passage_text.len());
    let mut rest = template;
    while let Some(open) = rest.find("{{") {
        rendered.push_str(&rest[..open]);
        let after = &rest[open + 2..];
        let Some(close) = after.find("}}") else {
            return Err(Error::Validation(
                "Unterminated '{{' in prompt template.".to_owned(),
            ));
        };
        let name = after[..close].trim();
        // `extra_context` wins on collision, as in Python's
        // `{**base, **extra}` merge — so it is consulted first.
        let value = match extra_context.and_then(|ctx| ctx.get(name)) {
            Some(value) => render_value(value),
            None => match name {
                "passage_text" => passage_text.to_owned(),
                "entity_hints" => entity_hints.to_owned(),
                // StrictUndefined: the template names a variable nobody
                // supplied. Fail naming it so the caller can retry with it.
                _ => return Err(Error::Validation(format!("'{name}' is undefined"))),
            },
        };
        rendered.push_str(&value);
        rest = &after[close + 2..];
    }
    rendered.push_str(rest);
    Ok(rendered)
}

/// Extra-context values render raw when they are strings; anything else
/// renders as JSON. (Jinja2 `str()` would print `True`/`None`; authored
/// extras are strings, so only that path is pinned.)
fn render_value(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        _ => value.to_string(),
    }
}

/// Record type definitions by id, in declaration order.
///
/// Takes the schema's `schema_def` mapping (the `schema` alias payload of an
/// `ExtractionSchema`, or the parsed YAML mapping). `serde_json::Map` keeps
/// insertion order (`preserve_order`), so declaration order survives.
pub fn record_type_definitions(schema_def: &Map<String, Value>) -> Result<Map<String, Value>> {
    let mut definitions = Map::new();
    let record_types = match schema_def.get("record_types") {
        None => return Ok(definitions),
        Some(Value::Array(items)) => items,
        // `schema_def.get("record_types", [])` in Python: a missing key is an
        // empty schema (rejected later by `build_output_schema`); a
        // present-but-not-a-list value has no defined behavior to mirror.
        Some(_) => {
            return Err(Error::Validation(
                "An extraction schema's 'record_types' must be a list.".to_owned(),
            ));
        }
    };
    for record_type in record_types {
        let Some(record_type) = record_type.as_object() else {
            return Err(Error::Validation(
                "Every record_type must be an object with an 'id'.".to_owned(),
            ));
        };
        let id = record_type
            .get("id")
            .and_then(Value::as_str)
            .filter(|id| !id.is_empty());
        let Some(id) = id else {
            return Err(Error::Validation(
                "Every record_type needs an 'id'.".to_owned(),
            ));
        };
        if definitions.contains_key(id) {
            return Err(Error::Validation(format!(
                "Record type '{id}' is declared twice."
            )));
        }
        definitions.insert(id.to_owned(), Value::Object(record_type.clone()));
    }
    Ok(definitions)
}

/// Fields of this record type declared as verbatim quotations: found by their
/// declared `type`, never by their name.
pub fn evidence_field_names(record_type: &Map<String, Value>) -> Vec<String> {
    field_names_where(record_type, |spec| {
        spec.get("type").and_then(Value::as_str) == Some(EVIDENCE_TYPE)
    })
}

/// Fields of this record type the model must supply.
pub fn required_field_names(record_type: &Map<String, Value>) -> Vec<String> {
    field_names_where(record_type, |spec| {
        spec.get("required")
            .is_some_and(|flag| !is_falsy_value(flag))
    })
}

fn field_names_where(
    record_type: &Map<String, Value>,
    mut keep: impl FnMut(&Map<String, Value>) -> bool,
) -> Vec<String> {
    record_type
        .get("fields")
        .and_then(Value::as_object)
        .map(|fields| {
            fields
                .iter()
                .filter(|(_, spec)| spec.as_object().is_some_and(&mut keep))
                .map(|(name, _)| name.clone())
                .collect()
        })
        .unwrap_or_default()
}

/// JSON Schema for the model's structured output.
///
/// Field definitions merge across record types into one `fields` object,
/// because a per-type `oneOf` is supported unevenly across providers and a
/// record that fails provider-side validation comes back as an opaque error.
/// [`crate::validation::validate_records`], where a violation can name the
/// record and the field it is missing.
pub fn build_output_schema(schema_def: &Map<String, Value>) -> Result<Value> {
    let definitions = record_type_definitions(schema_def)?;
    if definitions.is_empty() {
        return Err(Error::Validation(
            "An extraction schema needs at least one record_type.".to_owned(),
        ));
    }

    let mut merged: Map<String, Value> = Map::new();
    for (rt_id, record_type) in &definitions {
        let fields = record_type
            .as_object()
            .and_then(|rt| rt.get("fields"))
            .and_then(Value::as_object);
        for (name, spec) in fields.into_iter().flatten() {
            let candidate = field_json_schema(name, spec.as_object())?;
            // Proof the `else` below fires only for absent keys: `merged`
            // holds only `Value::Object` — every insert wraps a `Map`
            // (`candidate` here, `reconciled` from `merge_field`) — so a
            // present entry always matches the `Some` arm. The old defensive
            // `Some(non-object)` re-insert is removed: no public input can
            // place a non-object here, so removing it changes no behavior.
            if let Some(Value::Object(existing)) = merged.remove(name) {
                let reconciled = merge_field(name, rt_id, &existing, &candidate)?;
                // `Map::remove` is order-destroying (`swap_remove`); re-insert
                // so a re-declared field keeps first-declaration position.
                merged.insert(name.clone(), Value::Object(reconciled));
            } else {
                merged.insert(name.clone(), Value::Object(candidate));
            }
        }
    }

    let record_ids: Vec<Value> = definitions
        .keys()
        .map(|id| Value::String(id.clone()))
        .collect();
    Ok(serde_json::json!({
        "type": "object",
        "properties": {
            "records": {
                "type": "array",
                "description": "One entry per extracted record. Return an empty array when the passage supports none.",
                "items": {
                    "type": "object",
                    "properties": {
                        "record_type": {
                            "type": "string",
                            "enum": record_ids,
                            "description": "Which record type this entry is.",
                        },
                        "fields": {
                            "type": "object",
                            "description": "The fields declared by this record's type. Omit fields belonging to other types.",
                            "properties": merged,
                        },
                    },
                    "required": ["record_type", "fields"],
                },
            }
        },
        "required": ["records"],
    }))
}

/// JSON Schema fragment for one declared field. A missing spec is the
/// `{"type": "string"}` default, matching `spec.get("type", "string")`.
fn field_json_schema(name: &str, spec: Option<&Map<String, Value>>) -> Result<Map<String, Value>> {
    let declared = spec
        .and_then(|spec| spec.get("type"))
        .and_then(Value::as_str)
        .unwrap_or("string");
    let mut description = spec
        .and_then(|spec| spec.get("description"))
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();
    if declared == EVIDENCE_TYPE && description.is_empty() {
        description = "Quote the passage exactly, word for word.".to_owned();
    }
    if declared == "entity_ref" {
        // A model cannot know this corpus's UUIDs. Asking for one — as an
        // early schema did, with "Resolved entity; leave null if ambiguous" —
        // guarantees the field is null or invented. Ask for the name and
        // resolve it here, where the entity store is.
        description = "The name as written in the passage. Do not supply an identifier.".to_owned();
    }
    if declared == "fuzzy_date" {
        description = format!(
            "{description} Give the date as the passage words it \
            (\"the 15th ult.\", \"May 1862\"); it is interpreted afterwards."
        )
        .trim()
        .to_owned();
    }
    let mut prop = Map::new();
    prop.insert(
        "type".to_owned(),
        Value::String(json_base_type(declared).to_owned()),
    );
    prop.insert("description".to_owned(), Value::String(description));
    if declared == "enum" {
        // A `values` entry that is not a list has no defined behavior in
        // Python (`list(...)` would split a string into chars); the typed
        // boundary reads it as empty.
        let values = spec
            .and_then(|spec| spec.get("values"))
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        prop.insert("enum".to_owned(), Value::Array(values));
    }
    if declared == "number" {
        if let Some(bounds) = spec.and_then(|spec| spec.get("range")) {
            if !is_falsy_value(bounds) {
                let bounds = bounds.as_array().filter(|bounds| bounds.len() == 2);
                let Some(bounds) = bounds else {
                    // Python indexes `bounds[0]`/`bounds[1]` unguarded and
                    // `IndexError`s on a short range; a long one silently
                    // keeps its first two. The typed boundary rejects
                    // anything but the pair instead of guessing.
                    return Err(Error::Validation(format!(
                        "Field '{name}' range must be a [minimum, maximum] pair."
                    )));
                };
                prop.insert("minimum".to_owned(), bounds[0].clone());
                prop.insert("maximum".to_owned(), bounds[1].clone());
            }
        }
    }
    if declared == "array" {
        prop.insert("items".to_owned(), serde_json::json!({"type": "string"}));
    }
    Ok(prop)
}

/// Reconcile one field name declared by two record types.
///
/// Widening an enum or a numeric range is safe — the per-type check
/// downstream still holds each record to its own type's declaration.
/// Disagreeing on the base type is not recoverable, and guessing would send
/// the model a shape that contradicts half the schema.
fn merge_field(
    name: &str,
    record_type: &str,
    existing: &Map<String, Value>,
    candidate: &Map<String, Value>,
) -> Result<Map<String, Value>> {
    let existing_type = existing
        .get("type")
        .and_then(Value::as_str)
        .unwrap_or("string");
    let candidate_type = candidate
        .get("type")
        .and_then(Value::as_str)
        .unwrap_or("string");
    if existing_type != candidate_type {
        return Err(Error::Validation(format!(
            "Field '{name}' is declared as {existing_type} by one record type \
            and {candidate_type} by '{record_type}'. Give them different \
            names, or the same type."
        )));
    }
    let mut merged = existing.clone();
    if existing.contains_key("enum") || candidate.contains_key("enum") {
        let mut values = existing
            .get("enum")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        for value in candidate
            .get("enum")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            if !values.contains(value) {
                values.push(value.clone());
            }
        }
        merged.insert("enum".to_owned(), Value::Array(values));
    }
    // A one-sided bound is that type's own requirement, not a merge input:
    // ranges widen only when BOTH sides declare one. `minimum`/`maximum` are
    // inserted only as a pair from one `range` (see `_field_json_schema`),
    // so two present minimums prove two present maximums and the second
    // lookup cannot miss — no branch, matching Python's unguarded
    // `existing["maximum"]` without its `KeyError` on hand-built schemas
    // (the typed boundary asks for both keys via `range` instead).
    if let (Some(a_min), Some(b_min)) = (existing.get("minimum"), candidate.get("minimum")) {
        merged.insert("minimum".to_owned(), json_num_min(a_min, b_min));
        merged.insert(
            "maximum".to_owned(),
            json_num_max(&existing["maximum"], &candidate["maximum"]),
        );
    }
    Ok(merged)
}

/// The smaller of two JSON numbers, keeping the winner's original JSON
/// representation (so `0` stays an integer). Non-numbers — unreachable from
/// [`field_json_schema`], which only sets numeric bounds — compare unequal
/// and keep the existing side.
fn json_num_min(a: &Value, b: &Value) -> Value {
    match (a.as_f64(), b.as_f64()) {
        (Some(x), Some(y)) => {
            if x <= y {
                a.clone()
            } else {
                b.clone()
            }
        }
        _ => a.clone(),
    }
}

/// The larger of two JSON numbers; see [`json_num_min`].
fn json_num_max(a: &Value, b: &Value) -> Value {
    match (a.as_f64(), b.as_f64()) {
        (Some(x), Some(y)) => {
            if x >= y {
                a.clone()
            } else {
                b.clone()
            }
        }
        _ => a.clone(),
    }
}

/// Python truthiness over JSON values, shared with the validator's
/// falsy-required check.
///
/// Lives here — rather than in `validation` — so the import direction mirrors
/// Python (`validation` imports `schemas`, never the reverse).
pub fn is_falsy_value(value: &Value) -> bool {
    match value {
        Value::Null => true,
        Value::Bool(flag) => !flag,
        // `0` and `0.0` (including `-0.0`, which compares equal) are both
        // falsy in Python; any other number is truthy.
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                i == 0
            } else if let Some(u) = n.as_u64() {
                u == 0
            } else {
                n.as_f64().is_some_and(|f| f == 0.0)
            }
        }
        Value::String(text) => text.is_empty(),
        Value::Array(items) => items.is_empty(),
        Value::Object(fields) => fields.is_empty(),
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    fn schema_def(value: Value) -> Map<String, Value> {
        value
            .as_object()
            .cloned()
            .expect("test schema is an object")
    }

    fn claim_schema() -> Map<String, Value> {
        schema_def(json!({
            "record_types": [
                {
                    "id": "claim",
                    "fields": {
                        "assertion": {"type": "string", "required": true},
                        "quote": {"type": "evidence_span", "required": true},
                        "stance": {"type": "enum", "values": ["affirms", "denies"]},
                        "confidence": {"type": "number", "range": [0, 1]},
                    },
                },
                {
                    "id": "cross_reference",
                    "fields": {
                        "target": {"type": "string"},
                        "quote": {"type": "evidence_span", "required": true},
                    },
                },
            ]
        }))
    }

    fn output_fields(schema: &Map<String, Value>) -> Map<String, Value> {
        let schema = build_output_schema(schema).expect("test schema builds");
        schema["properties"]["records"]["items"]["properties"]["fields"]["properties"]
            .as_object()
            .cloned()
            .expect("output has flat fields")
    }
    /// Variant assertion without branches (see budget's `assert_refused`).
    fn assert_validation(err: &Error) {
        assert_eq!(
            std::mem::discriminant(err),
            std::mem::discriminant(&Error::Validation(String::new()))
        );
    }
    fn validation_message(err: Error) -> String {
        // Every failure below is `Error::Validation`; the discriminant pins
        // that, and `Display` renders it verbatim — exactly the message.
        assert_validation(&err);
        err.to_string()
    }

    #[test]
    fn parse_schema_yaml_basic() {
        let result = parse_schema_yaml(
            "id: test_schema\nversion: 1\ndescription: Test\nowner: core\nrecord_types:\n  - id: test_record\n    fields:\n      name:\n        type: string\n        required: true\nprompt: \"Extract from {{ passage_text }}\"\n",
        )
        .expect("parses");
        assert_eq!(result["id"], json!("test_schema"));
        assert_eq!(result["version"], json!(1));
        assert_eq!(result["record_types"].as_array().expect("list").len(), 1);
    }

    #[test]
    fn parse_schema_yaml_empty_is_null() {
        assert_eq!(parse_schema_yaml("").expect("empty"), Value::Null);
        assert_eq!(parse_schema_yaml("  \n ").expect("blank"), Value::Null);
    }

    #[test]
    fn render_prompt_basic() {
        let result = render_prompt("Extract from: {{ passage_text }}", "Hello world", "", None)
            .expect("renders");
        assert!(result.contains("Hello world"));
    }

    #[test]
    fn render_prompt_entity_hints() {
        let result = render_prompt(
            "{{ passage_text }}\nHints: {{ entity_hints }}",
            "text",
            "McClellan, Barlow",
            None,
        )
        .expect("renders");
        assert!(result.contains("McClellan, Barlow"));
    }

    #[test]
    fn render_prompt_extra_context() {
        let mut extra = Map::new();
        extra.insert("custom_field".to_owned(), json!("extra"));
        let result = render_prompt(
            "{{ passage_text }} - {{ custom_field }}",
            "text",
            "",
            Some(&extra),
        )
        .expect("renders");
        assert!(result.contains("extra"));
    }

    #[test]
    fn render_prompt_entity_hints_default_empty() {
        let result = render_prompt("[{{ entity_hints }}]", "text", "", None).expect("renders");
        assert_eq!(result, "[]");
    }

    #[test]
    fn render_prompt_extra_wins_on_collision() {
        let mut extra = Map::new();
        extra.insert("passage_text".to_owned(), json!("override"));
        let result =
            render_prompt("{{ passage_text }}", "base", "", Some(&extra)).expect("renders");
        assert_eq!(result, "override");
    }

    #[test]
    fn render_prompt_missing_variable_names_it() {
        let err = render_prompt("{{ passage_text }} {{ absent }}", "text", "", None)
            .expect_err("missing var fails");
        assert!(err.to_string().contains("absent"), "names the var: {err}");
    }

    #[test]
    fn build_output_schema_each_record_names_its_own_type() {
        let schema = build_output_schema(&claim_schema()).expect("builds");
        let item = &schema["properties"]["records"]["items"];
        assert_eq!(item["required"], json!(["record_type", "fields"]));
        assert_eq!(
            item["properties"]["record_type"]["enum"],
            json!(["claim", "cross_reference"])
        );
        assert_eq!(
            item["properties"]["record_type"]["description"],
            json!("Which record type this entry is.")
        );
    }

    #[test]
    fn build_output_schema_fields_are_a_flat_object() {
        let fields = output_fields(&claim_schema());
        assert_eq!(fields["assertion"]["type"], json!("string"));
        assert_eq!(fields["quote"]["type"], json!("string"));
        assert_eq!(fields["stance"]["enum"], json!(["affirms", "denies"]));
        assert_eq!(fields["confidence"]["minimum"], json!(0));
        assert_eq!(fields["confidence"]["maximum"], json!(1));
    }

    #[test]
    fn build_output_schema_records_description_verbatim() {
        let schema = build_output_schema(&claim_schema()).expect("builds");
        assert_eq!(
            schema["properties"]["records"]["description"],
            json!("One entry per extracted record. Return an empty array when the passage supports none.")
        );
        assert_eq!(
            schema["properties"]["records"]["items"]["properties"]["fields"]["description"],
            json!(
                "The fields declared by this record's type. Omit fields belonging to other types."
            )
        );
    }

    #[test]
    fn build_output_schema_unknown_field_type_becomes_a_string() {
        // The `date` incident: declared types used to pass through verbatim,
        // so `{"type": "date"}` reached the provider, which rejects it.
        let schema = schema_def(json!({
            "record_types": [
                {"id": "note", "fields": {
                    "when": {"type": "date"},
                    "quote": {"type": "evidence_span"},
                }},
            ]
        }));
        let fields = output_fields(&schema);
        assert_eq!(fields["when"]["type"], json!("string"));
    }
    #[test]
    fn build_output_schema_scalar_types_map_verbatim() {
        // `integer` and `boolean` have dedicated JSON Schema types; only
        // unlisted declarations (like `date`) fall back to `string`.
        let schema = schema_def(json!({
            "record_types": [
                {"id": "note", "fields": {
                    "n": {"type": "integer"},
                    "flag": {"type": "boolean"},
                    "quote": {"type": "evidence_span"},
                }},
            ]
        }));
        let fields = output_fields(&schema);
        assert_eq!(fields["n"]["type"], json!("integer"));
        assert_eq!(fields["flag"]["type"], json!("boolean"));
    }

    #[test]
    fn parse_schema_yaml_malformed_is_rejected() {
        let err = parse_schema_yaml("{unclosed: [bracket").expect_err("malformed fails");
        assert!(
            !validation_message(err).is_empty(),
            "carries the YAML error"
        );
    }

    #[test]
    fn build_output_schema_surfaces_definition_errors() {
        // The `?` propagates `record_type_definitions` failures instead of
        // building over them: a duplicate id never reaches the merge.
        let schema = schema_def(json!({"record_types": [{"id": "a"}, {"id": "a"}]}));
        let err = build_output_schema(&schema).expect_err("dup fails");
        assert_eq!(
            validation_message(err),
            "Record type 'a' is declared twice."
        );
    }

    #[test]
    fn build_output_schema_two_record_types_may_share_a_field() {
        let fields = output_fields(&claim_schema());
        assert_eq!(fields["quote"]["type"], json!("string"));
        assert_eq!(
            fields["quote"]["description"],
            json!("Quote the passage exactly, word for word.")
        );
    }

    #[test]
    fn build_output_schema_incompatible_shared_field_is_rejected() {
        let schema = schema_def(json!({
            "record_types": [
                {"id": "a", "fields": {
                    "n": {"type": "number"}, "q": {"type": "evidence_span"}}},
                {"id": "b", "fields": {
                    "n": {"type": "string"}, "q": {"type": "evidence_span"}}},
            ]
        }));
        let err = build_output_schema(&schema).expect_err("type clash fails");
        assert!(err.to_string().contains("'n'"), "names the field: {err}");
    }

    #[test]
    fn build_output_schema_with_no_record_types_is_rejected() {
        let schema = schema_def(json!({"record_types": []}));
        build_output_schema(&schema).expect_err("empty schema fails");
    }

    #[test]
    fn record_type_definitions_missing_id_is_rejected() {
        let schema = schema_def(json!({"record_types": [{"fields": {}}]}));
        let err = record_type_definitions(&schema).expect_err("missing id fails");
        assert_eq!(validation_message(err), "Every record_type needs an 'id'.");
    }

    #[test]
    fn record_type_definitions_duplicate_id_is_rejected() {
        let schema = schema_def(json!({"record_types": [{"id": "a"}, {"id": "a"}]}));
        let err = record_type_definitions(&schema).expect_err("dup fails");
        assert_eq!(
            validation_message(err),
            "Record type 'a' is declared twice."
        );
    }

    #[test]
    fn record_type_definitions_keep_declaration_order() {
        let schema = schema_def(json!({"record_types": [{"id": "b"}, {"id": "a"}]}));
        let defs = record_type_definitions(&schema).expect("builds");
        let ids: Vec<&String> = defs.keys().collect();
        assert_eq!(ids, [&"b".to_owned(), &"a".to_owned()]);
    }

    #[test]
    fn evidence_fields_found_by_declared_type_not_by_name() {
        let record_type = schema_def(json!({"fields": {
            "quote": {"type": "evidence_span"},
            "evidence_of_nothing": {"type": "string"},
        }}));
        assert_eq!(evidence_field_names(&record_type), ["quote"]);
    }

    #[test]
    fn entity_ref_description_asks_for_the_name() {
        let schema = schema_def(json!({
            "record_types": [{"id": "r", "fields": {
                "who": {"type": "entity_ref"},
                "quote": {"type": "evidence_span"},
            }}]
        }));
        let fields = output_fields(&schema);
        assert_eq!(
            fields["who"]["description"],
            json!("The name as written in the passage. Do not supply an identifier.")
        );
    }

    #[test]
    fn fuzzy_date_description_appends_the_suffix() {
        let schema = schema_def(json!({
            "record_types": [{"id": "r", "fields": {
                "when": {"type": "fuzzy_date", "description": "The letter date."},
                "quote": {"type": "evidence_span"},
            }}]
        }));
        let fields = output_fields(&schema);
        assert_eq!(
            fields["when"]["description"],
            json!("The letter date. Give the date as the passage words it (\"the 15th ult.\", \"May 1862\"); it is interpreted afterwards.")
        );
    }

    #[test]
    fn array_fields_carry_string_items() {
        let schema = schema_def(json!({
            "record_types": [{"id": "r", "fields": {
                "tags": {"type": "array"},
                "quote": {"type": "evidence_span"},
            }}]
        }));
        let fields = output_fields(&schema);
        assert_eq!(fields["tags"]["items"], json!({"type": "string"}));
    }

    #[test]
    fn enum_values_union_order_preserving_across_types() {
        let schema = schema_def(json!({
            "record_types": [
                {"id": "a", "fields": {
                    "stance": {"type": "enum", "values": ["affirms", "denies"]},
                    "q": {"type": "evidence_span"}}},
                {"id": "b", "fields": {
                    "stance": {"type": "enum", "values": ["denies", "unclear"]},
                    "q": {"type": "evidence_span"}}},
            ]
        }));
        let fields = output_fields(&schema);
        assert_eq!(
            fields["stance"]["enum"],
            json!(["affirms", "denies", "unclear"])
        );
    }

    #[test]
    fn number_ranges_widen_only_when_both_sides_declare_one() {
        let schema = schema_def(json!({
            "record_types": [
                {"id": "a", "fields": {
                    "n": {"type": "number", "range": [0, 1]},
                    "q": {"type": "evidence_span"}}},
                {"id": "b", "fields": {
                    "n": {"type": "number", "range": [0, 5]},
                    "q": {"type": "evidence_span"}}},
            ]
        }));
        let fields = output_fields(&schema);
        assert_eq!(fields["n"]["minimum"], json!(0));
        assert_eq!(fields["n"]["maximum"], json!(5));

        // One-sided bound: the declaring type's own requirement survives
        // untouched, with no merge input from the other side.
        let schema = schema_def(json!({
            "record_types": [
                {"id": "a", "fields": {
                    "n": {"type": "number", "range": [2, 4]},
                    "q": {"type": "evidence_span"}}},
                {"id": "b", "fields": {
                    "n": {"type": "number"},
                    "q": {"type": "evidence_span"}}},
            ]
        }));
        let fields = output_fields(&schema);
        assert_eq!(fields["n"]["minimum"], json!(2));
        assert_eq!(fields["n"]["maximum"], json!(4));
    }

    #[test]
    fn render_prompt_unterminated_placeholder_is_rejected() {
        let err = render_prompt("Extract from {{ passage_text", "text", "", None)
            .expect_err("unterminated fails");
        assert_eq!(
            validation_message(err),
            "Unterminated '{{' in prompt template."
        );
    }

    #[test]
    fn render_prompt_non_string_extra_renders_as_json() {
        // Jinja2 `str(42)` is `"42"`; authored extras are strings, so only
        // the number case is pinned here.
        let mut extra = Map::new();
        extra.insert("limit".to_owned(), json!(42));
        let result = render_prompt(
            "Take {{ limit }} records: {{ passage_text }}",
            "text",
            "",
            Some(&extra),
        )
        .expect("renders");
        assert_eq!(result, "Take 42 records: text");
    }

    #[test]
    fn record_type_definitions_missing_key_is_empty() {
        // Python `schema_def.get("record_types", [])`: a missing key is an
        // empty schema, rejected later by `build_output_schema`.
        let schema = schema_def(json!({"id": "note"}));
        assert!(record_type_definitions(&schema).expect("builds").is_empty());
        let err = build_output_schema(&schema).expect_err("empty schema fails");
        assert_eq!(
            validation_message(err),
            "An extraction schema needs at least one record_type."
        );
    }

    #[test]
    fn record_type_definitions_non_list_is_rejected() {
        // A present-but-not-a-list value has no defined Python behavior to
        // mirror; the typed boundary rejects it instead of guessing.
        let schema = schema_def(json!({"record_types": {"id": "a"}}));
        let err = record_type_definitions(&schema).expect_err("non-list fails");
        assert_eq!(
            validation_message(err),
            "An extraction schema's 'record_types' must be a list."
        );
    }

    #[test]
    fn record_type_definitions_non_object_entry_is_rejected() {
        let schema = schema_def(json!({"record_types": ["claim"]}));
        let err = record_type_definitions(&schema).expect_err("string entry fails");
        assert_eq!(
            validation_message(err),
            "Every record_type must be an object with an 'id'."
        );
    }

    #[test]
    fn record_type_definitions_blank_or_untyped_id_is_rejected() {
        // Python `if not rt_id`: empty, null, and non-string ids are all missing.
        for id in [json!(""), json!(null), json!(3)] {
            let schema = schema_def(json!({"record_types": [{"id": id}]}));
            let err = record_type_definitions(&schema).expect_err("bad id fails");
            assert_eq!(validation_message(err), "Every record_type needs an 'id'.");
        }
    }

    #[test]
    fn number_range_must_be_a_minimum_maximum_pair() {
        // Python indexes `bounds[0]`/`bounds[1]` unguarded: a short range
        // `IndexError`s, a long one silently keeps its first two. The typed
        // boundary rejects anything but the pair instead of guessing.
        for range in [json!([0]), json!([0, 1, 2]), json!(5)] {
            let schema = schema_def(json!({
                "record_types": [{"id": "r", "fields": {
                    "n": {"type": "number", "range": range},
                    "quote": {"type": "evidence_span"},
                }}]
            }));
            let err = build_output_schema(&schema).expect_err("bad range fails");
            assert_eq!(
                validation_message(err),
                "Field 'n' range must be a [minimum, maximum] pair."
            );
        }
    }

    #[test]
    fn number_without_a_range_carries_no_bounds() {
        // Python `if declared == "number" and (bounds := spec.get("range"))`:
        // absent or falsy bounds leave the property unbounded.
        for range in [None, Some(json!([]))] {
            let mut spec = json!({"type": "number"});
            if let Some(range) = range {
                spec["range"] = range;
            }
            let schema = schema_def(json!({
                "record_types": [{"id": "r", "fields": {
                    "n": spec,
                    "quote": {"type": "evidence_span"},
                }}]
            }));
            let fields = output_fields(&schema);
            assert_eq!(fields["n"]["type"], json!("number"));
            assert!(fields["n"].get("minimum").is_none());
            assert!(fields["n"].get("maximum").is_none());
        }
    }

    #[test]
    fn number_ranges_widen_taking_each_winner() {
        // `min`/`max` exactly as Python: the smaller minimum and the larger
        // maximum win, keeping the winner's original JSON representation.
        let schema = schema_def(json!({
            "record_types": [
                {"id": "a", "fields": {
                    "n": {"type": "number", "range": [2, 9]},
                    "q": {"type": "evidence_span"}}},
                {"id": "b", "fields": {
                    "n": {"type": "number", "range": [0, 5]},
                    "q": {"type": "evidence_span"}}},
            ]
        }));
        let fields = output_fields(&schema);
        assert_eq!(fields["n"]["minimum"], json!(0));
        assert_eq!(fields["n"]["maximum"], json!(9));
    }

    #[test]
    fn merge_keeps_the_existing_side_for_non_numeric_bounds() {
        // A range of non-numbers is a malformed schema (bounds must be
        // numbers); rather than compare across types, the merge keeps the
        // existing side.
        let schema = schema_def(json!({
            "record_types": [
                {"id": "a", "fields": {
                    "n": {"type": "number", "range": ["b", "c"]},
                    "q": {"type": "evidence_span"}}},
                {"id": "b", "fields": {
                    "n": {"type": "number", "range": ["a", "d"]},
                    "q": {"type": "evidence_span"}}},
            ]
        }));
        let fields = output_fields(&schema);
        assert_eq!(fields["n"]["minimum"], json!("b"));
        assert_eq!(fields["n"]["maximum"], json!("c"));
    }

    #[test]
    fn huge_integers_are_truthy() {
        // `u64::MAX` fits no `i64`, so it reaches the `u64` arm: nonzero and
        // truthy, exactly as Python `bool(2**64 - 1)`.
        assert!(!is_falsy_value(&Value::from(u64::MAX)));
    }
}
