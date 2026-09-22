//! MCP tool-catalogue shaping without the SDK: registry, request parsing,
//! and response envelopes.
//!
//! Ports the SDK-independent core of `mcp/catalog.py` ([`ToolCatalog`]),
//! `mcp/dispatch.py` ([`validate_input`], [`resolve_tool_id`]), and
//! `mcp/errors.py` ([`envelope`], [`failed`], [`unknown_tool`],
//! [`validation_error`], [`permission_denied`]).
//!
//! What stays Python and why: `server.py` (the `mcp` SDK stdio transport),
//! `_select_clients` (`inspect.signature` over live callables), `_register_all`
//! and the `_build_*_entries` snapshots (they close over SDK `Tool` objects,
//! the live container, and the plugin loader), per-tool schemas and handlers
//! (service-bound), and `_make_core_handler` (validate-call-envelop over live
//! handlers — its constructors live here).

use std::collections::HashMap;

use serde_json::{Map, Value};
/// Every core tool, in `CORE_TOOL_MODULES` listing order. Names only:
/// descriptions and schemas live with the SDK-bound handlers.
pub const CORE_TOOL_NAMES: &[&str] = &[
    "find_passages",
    "get_document",
    "get_passage_context",
    "similar_to",
    "get_document_outline",
    "read_node",
    "locate_passage",
    "find_lemma",
    "resolve_entity",
    "get_entity",
    "find_mentions",
    "events",
    "timeline_compare",
    "extract",
    "list_extraction_schemas",
    "query_extractions",
    "provenance_of",
    "corpus_stats",
    "llm_usage",
    "citations",
    "anchor_context",
    "claim_audit",
    "upsert_entity",
    "upsert_event",
    "verify_quote",
    "upsert_edge",
    "claim_upsert",
    "list_available_filters",
    "search_sources",
    "ingest_execute",
    "work_verify",
    "work_citations",
    "work_cite_entry",
    "work_render",
    "work_create",
    "work_get",
    "work_block_upsert",
    "work_cite",
    "work_link",
    "work_validate",
    "work_trace",
    "work_freeze",
];

/// Mutable tool registry the wire closures read through, never capture.
///
/// Installing a pack is a [`ToolCatalog::replace_packs`] away — no restart.
/// Handlers are generic so the catalogue owns the ordering, lookup, and
/// version rules without touching the SDK `Tool` type.
pub struct ToolCatalog<H> {
    core_defs: Vec<String>,
    core_handlers: HashMap<String, H>,
    pack_defs: Vec<String>,
    pack_handlers: HashMap<String, H>,
    listeners: Vec<Box<dyn Fn()>>,
    /// Bumped by every [`ToolCatalog::notify_changed`].
    pub changed_version: u64,
}

impl<H> ToolCatalog<H> {
    /// Empty catalogue: no core slice, no packs, version zero.
    pub fn new() -> Self {
        Self {
            core_defs: Vec::new(),
            core_handlers: HashMap::new(),
            pack_defs: Vec::new(),
            pack_handlers: HashMap::new(),
            listeners: Vec::new(),
            changed_version: 0,
        }
    }

    /// Fix the core slice. Called once at startup; packs never touch it.
    pub fn set_core(&mut self, defs: Vec<String>, handlers: HashMap<String, H>) {
        self.core_defs = defs;
        self.core_handlers = handlers;
    }

    /// Swap the whole pack slice atomically; core entries are untouched.
    /// Takes the whole pack set — not one entry — so an unload is as
    /// expressible as a load, and a half-applied load cannot leave a handler
    /// whose def was never listed.
    pub fn replace_packs(&mut self, defs: Vec<String>, handlers: HashMap<String, H>) {
        self.pack_defs = defs;
        self.pack_handlers = handlers;
    }

    /// Replace one core entry by name, leaving the rest of the slice alone.
    /// The handler lands even when the name was never listed — that is the
    /// Python's behavior (`handlers[name] = handler` runs unconditionally),
    /// and the one caller (`find_passages` schema refresh) depends on it.
    /// Names are the whole def here (descriptions/schemas live SDK-side, so
    /// a same-name replacement is identity); only the handler map moves.
    pub fn update_core_tool(&mut self, name: &str, handler: H) {
        self.core_handlers.insert(name.to_owned(), handler);
    }

    /// Core defs then pack defs, in listing order.
    pub fn defs(&self) -> Vec<&str> {
        self.core_defs
            .iter()
            .chain(self.pack_defs.iter())
            .map(String::as_str)
            .collect()
    }

    /// The handler for a tool name. Packs win over core on collision.
    pub fn handler(&self, name: &str) -> Option<&H> {
        self.pack_handlers
            .get(name)
            .or_else(|| self.core_handlers.get(name))
    }

    /// Register for change events. A listener is a zero-arg callable;
    /// transports use it to push `notifications/tools/list_changed`.
    pub fn subscribe(&mut self, listener: Box<dyn Fn()>) {
        self.listeners.push(listener);
    }

    /// Bump the version and fan out to the listeners registered so far —
    /// listeners added during the fan-out wait for the next change, exactly
    /// like the Python `for listener in list(self._listeners)`.
    pub fn notify_changed(&mut self) {
        self.changed_version += 1;
        let count = self.listeners.len();
        for i in 0..count {
            self.listeners[i]();
        }
    }
}

impl<H> Default for ToolCatalog<H> {
    fn default() -> Self {
        Self::new()
    }
}

/// Whether a registered tool id answers a requested one. Plugin tools
/// register under dotted ids (`acad.discover_by_doi`) but agent-facing calls
/// use the underscored MCP name (`acad_discover_by_doi`); either form matches.
pub fn match_tool_id(registered: &str, requested: &str) -> bool {
    registered == requested || registered.replace('.', "_") == requested
}

/// The registered id a request resolves to, if any. First match wins, in
/// registration order — mirroring the dispatch loop.
pub fn resolve_tool_id<'r>(registered: &'r [String], requested: &str) -> Option<&'r str> {
    registered
        .iter()
        .find(|id| match_tool_id(id, requested))
        .map(String::as_str)
}

/// Lightweight JSON Schema validation: required fields, types, and enums.
///
/// Checks that every `required` field is present, and for each provided field
/// with a declared `type`/`enum` in `properties`, that the value conforms.
/// Returns an error message on failure, `None` when valid.
///
/// Shallow by decision, not by accident (WI-6, Option A): nested objects are
/// NOT descended into, array `items` are NOT checked, `format` is NOT
/// enforced, and `default` is NOT applied. Handlers must treat every nested
/// value as arbitrary JSON and re-check what they depend on. Fields without a
/// property spec pass through.
///
/// `bool` never counts as `integer` or `number` — JSON keeps them distinct,
/// so unlike the Python (which special-cases the `bool`-is-`int` subclass),
/// no guard is needed.
pub fn validate_input(schema: &Value, arguments: &Map<String, Value>) -> Option<String> {
    if let Some(required) = schema.get("required").and_then(Value::as_array) {
        for field in required {
            if let Some(name) = field.as_str() {
                if !arguments.contains_key(name) {
                    return Some(format!("Missing required field: '{name}'"));
                }
            }
        }
    }
    let properties = schema.get("properties").and_then(Value::as_object);
    for (field, value) in arguments {
        let spec = properties
            .and_then(|props| props.get(field))
            .and_then(Value::as_object);
        let Some(spec) = spec else { continue };
        if let Some(expected) = spec.get("type").and_then(Value::as_str) {
            let ok = match expected {
                "string" => value.is_string(),
                "integer" => value.is_i64() || value.is_u64(),
                "number" => value.is_i64() || value.is_u64() || value.is_f64(),
                "boolean" => value.is_boolean(),
                "array" => value.is_array(),
                "object" => value.is_object(),
                _ => true,
            };
            if !ok {
                return Some(format!("Field '{field}' must be of type {expected}"));
            }
        }
        if let Some(allowed) = spec.get("enum").and_then(Value::as_array) {
            if !allowed.iter().any(|candidate| candidate == value) {
                return Some(format!(
                    "Field '{field}' must be one of {}",
                    py_repr_list(allowed)
                ));
            }
        }
    }
    None
}

/// Python `repr` of a JSON value, for the `must be one of [...]` message.
/// Python renders the enum list with `repr`, so strings carry single quotes
/// (`['fast', 'slow']`), `True`/`False`/`None` are capitalized, and floats
/// use shortest round-trip — all matched here.
fn py_repr(value: &Value) -> String {
    match value {
        Value::Null => "None".to_owned(),
        Value::Bool(true) => "True".to_owned(),
        Value::Bool(false) => "False".to_owned(),
        Value::Number(n) => n.to_string(),
        Value::String(s) => format!("'{}'", py_escape_str(s)),
        Value::Array(items) => {
            let inner: Vec<String> = items.iter().map(py_repr).collect();
            format!("[{}]", inner.join(", "))
        }
        Value::Object(map) => {
            let inner: Vec<String> = map
                .iter()
                .map(|(k, v)| format!("'{}': {}", py_escape_str(k), py_repr(v)))
                .collect();
            format!("{{{}}}", inner.join(", "))
        }
    }
}

/// Python `repr` of a string body (without the quotes): backslash, quote,
/// and control escapes; printable non-ASCII passes through literally.
fn py_escape_str(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\'' => out.push_str("\\'"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 || c as u32 == 0x7f => {
                out.push_str(&format!("\\x{:02x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out
}

fn py_repr_list(allowed: &[Value]) -> String {
    let inner: Vec<String> = allowed.iter().map(py_repr).collect();
    format!("[{}]", inner.join(", "))
}

/// The one error shape the agent ever sees.
pub fn envelope(code: &str, message: &str, details: Option<Value>) -> Value {
    serde_json::json!({
        "error": {
            "code": code,
            "message": message,
            "details": details.unwrap_or(Value::Null),
        }
    })
}

/// A tool's catch-all. The code is derived, so it cannot drift from the name.
pub fn failed(tool_name: &str, message: &str) -> Value {
    envelope(&format!("{tool_name}_failed"), message, None)
}

/// The wire answer for an unregistered tool name.
pub fn unknown_tool(name: &str) -> Value {
    envelope("unknown_tool", &format!("Unknown tool: {name}"), None)
}

/// The wire answer for a request that fails [`validate_input`].
pub fn validation_error(message: &str) -> Value {
    envelope("validation_error", message, None)
}

/// `Plugin '{plugin}' lacks permission '{permission}'. ...`: the
/// `PermissionDenied` constructor message, so denials say so instead of
/// looking like crashes.
pub fn permission_denied_message(plugin: &str, permission: &str) -> String {
    format!(
        "Plugin '{plugin}' lacks permission '{permission}'. \
         Approve it in the permissions section of plugin.yaml before use."
    )
}

/// The wire answer for a denial: the code names it, the details name both
/// sides, the message says how to approve it.
pub fn permission_denied(plugin: &str, permission: &str) -> Value {
    envelope(
        "permission_denied",
        &permission_denied_message(plugin, permission),
        Some(serde_json::json!({"plugin": plugin, "permission": permission})),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::Cell;
    use std::rc::Rc;

    fn catalog() -> ToolCatalog<String> {
        let mut catalog = ToolCatalog::new();
        catalog.set_core(
            vec!["find_passages".to_owned(), "verify_quote".to_owned()],
            HashMap::from([
                ("find_passages".to_owned(), "core-fp".to_owned()),
                ("verify_quote".to_owned(), "core-vq".to_owned()),
            ]),
        );
        catalog
    }

    #[test]
    fn core_names_cover_every_dispatch_module_in_order() {
        assert_eq!(CORE_TOOL_NAMES.len(), 42);
        assert_eq!(CORE_TOOL_NAMES[0], "find_passages");
        assert_eq!(CORE_TOOL_NAMES[41], "work_freeze");
        assert!(CORE_TOOL_NAMES.contains(&"list_available_filters"));
        assert!(!CORE_TOOL_NAMES.contains(&"history_never"));
    }

    #[test]
    fn defs_list_core_then_packs() {
        let mut catalog = catalog();
        assert_eq!(catalog.defs(), vec!["find_passages", "verify_quote"]);
        catalog.replace_packs(
            vec!["history.correspondence_cadence".to_owned()],
            HashMap::from([(
                "history.correspondence_cadence".to_owned(),
                "pack-cc".to_owned(),
            )]),
        );
        assert_eq!(
            catalog.defs(),
            vec![
                "find_passages",
                "verify_quote",
                "history.correspondence_cadence"
            ]
        );
        // Unload is a replace with the empty set.
        catalog.replace_packs(Vec::new(), HashMap::new());
        assert_eq!(catalog.defs(), vec!["find_passages", "verify_quote"]);
        assert_eq!(catalog.handler("history.correspondence_cadence"), None);
    }

    #[test]
    fn packs_win_over_core_on_collision() {
        let mut catalog = catalog();
        catalog.replace_packs(
            vec!["verify_quote".to_owned()],
            HashMap::from([("verify_quote".to_owned(), "pack-vq".to_owned())]),
        );
        assert_eq!(
            catalog.handler("verify_quote").map(String::as_str),
            Some("pack-vq")
        );
        assert_eq!(
            catalog.handler("find_passages").map(String::as_str),
            Some("core-fp")
        );
        assert_eq!(catalog.handler("missing"), None);
    }

    #[test]
    fn update_core_tool_replaces_by_name_and_always_lands_the_handler() {
        let mut catalog = catalog();
        catalog.update_core_tool("verify_quote", "core-vq2".to_owned());
        assert_eq!(catalog.defs(), vec!["find_passages", "verify_quote"]);
        assert_eq!(
            catalog.handler("verify_quote").map(String::as_str),
            Some("core-vq2")
        );
        // Unknown name: defs untouched, handler still lands.
        catalog.update_core_tool("find_passages_v2", "core-fpv2".to_owned());
        assert_eq!(catalog.defs(), vec!["find_passages", "verify_quote"]);
        assert_eq!(
            catalog.handler("find_passages_v2").map(String::as_str),
            Some("core-fpv2")
        );
    }

    #[test]
    fn notify_changed_bumps_and_fans_out_to_the_registered() {
        let mut catalog: ToolCatalog<String> = ToolCatalog::new();
        assert_eq!(catalog.changed_version, 0);
        let calls = Rc::new(Cell::new(0u32));
        let probe = calls.clone();
        catalog.subscribe(Box::new(move || probe.set(probe.get() + 1)));
        catalog.notify_changed();
        assert_eq!(catalog.changed_version, 1);
        assert_eq!(calls.get(), 1);
        catalog.notify_changed();
        assert_eq!(catalog.changed_version, 2);
        assert_eq!(calls.get(), 2);
    }

    #[test]
    fn every_listener_fires_once_per_change() {
        let mut catalog: ToolCatalog<String> = ToolCatalog::new();
        let calls = Rc::new(Cell::new(0u32));
        for _ in 0..2 {
            let probe = calls.clone();
            catalog.subscribe(Box::new(move || probe.set(probe.get() + 1)));
        }
        catalog.notify_changed();
        assert_eq!(calls.get(), 2);
        // The version bumps even with no listeners to call.
        let mut bare: ToolCatalog<String> = ToolCatalog::default();
        bare.notify_changed();
        assert_eq!(bare.changed_version, 1);
    }

    #[test]
    fn tool_ids_match_dotted_or_underscored() {
        assert!(match_tool_id(
            "history.correspondence_cadence",
            "history.correspondence_cadence"
        ));
        assert!(match_tool_id(
            "history.correspondence_cadence",
            "history_correspondence_cadence"
        ));
        assert!(!match_tool_id(
            "history.correspondence_cadence",
            "history.find_missing_letters"
        ));
        let registered = vec![
            "history.find_missing_letters".to_owned(),
            "history.correspondence_cadence".to_owned(),
        ];
        assert_eq!(
            resolve_tool_id(&registered, "history_correspondence_cadence"),
            Some("history.correspondence_cadence")
        );
        assert_eq!(resolve_tool_id(&registered, "verify_quote"), None);
    }

    fn schema() -> Value {
        serde_json::json!({
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
                "ratio": {"type": "number"},
                "flag": {"type": "boolean"},
                "tags": {"type": "array"},
                "filters": {"type": "object"},
                "mode": {"type": "string", "enum": ["fast", "slow"]},
            },
            "required": ["query"],
        })
    }

    fn args(value: Value) -> Map<String, Value> {
        value.as_object().unwrap().clone()
    }

    #[test]
    fn valid_input_passes() {
        assert_eq!(
            validate_input(
                &schema(),
                &args(serde_json::json!({
                    "query": "hi",
                    "limit": 5,
                    "ratio": 1.5,
                    "flag": true,
                    "tags": ["a"],
                    "filters": {"language": "en"},
                    "mode": "fast",
                })),
            ),
            None
        );
    }

    #[test]
    fn untyped_and_unknown_typed_specs_constrain_nothing() {
        let schema = serde_json::json!({
            "properties": {
                "query": {},
                "limit": {"type": "frobnicator"},
                "query_spec_as_string": "string",
            },
        });
        assert_eq!(
            validate_input(
                &schema,
                &args(serde_json::json!({"query": 1, "limit": 2, "query_spec_as_string": 3})),
            ),
            None
        );
        // No `required` key, or a non-list one, means nothing is required.
        assert_eq!(
            validate_input(
                &serde_json::json!({"properties": {}}),
                &args(serde_json::json!({"anything": 1})),
            ),
            None
        );
        assert_eq!(
            validate_input(
                &serde_json::json!({"required": "query"}),
                &args(serde_json::json!({})),
            ),
            None
        );
        // Non-string required entries name nothing.
        assert_eq!(
            validate_input(
                &serde_json::json!({"required": [123]}),
                &args(serde_json::json!({})),
            ),
            None
        );
    }

    #[test]
    fn enum_repr_covers_every_json_shape() {
        let schema = serde_json::json!({
            "properties": {
                "mode": {"enum": [1, 1.5, true, false, null, "x", ["a"], {"k": "v"}]},
            },
        });
        let err = validate_input(&schema, &args(serde_json::json!({"mode": "other"}))).unwrap();
        assert_eq!(
            err,
            "Field 'mode' must be one of [1, 1.5, True, False, None, 'x', ['a'], {'k': 'v'}]"
        );
        assert_eq!(
            validate_input(&schema, &args(serde_json::json!({"mode": "x"}))),
            None
        );
    }

    #[test]
    fn enum_strings_escape_like_python_repr() {
        let schema = serde_json::json!({
            "properties": {"mode": {"enum": ["o'clock", "a\\b\nc\rd\te\x01f\x7fgé"]}},
        });
        let err = validate_input(&schema, &args(serde_json::json!({"mode": "other"}))).unwrap();
        assert_eq!(
            err,
            "Field 'mode' must be one of ['o\\'clock', 'a\\\\b\\nc\\rd\\te\\x01f\\x7fgé']"
        );
    }

    #[test]
    fn missing_required_names_the_field() {
        let err = validate_input(&schema(), &args(serde_json::json!({"limit": 5}))).unwrap();
        assert!(err.contains("query"), "{err}");
        assert_eq!(err, "Missing required field: 'query'");
    }

    #[test]
    fn wrong_types_name_the_field() {
        let err = validate_input(
            &schema(),
            &args(serde_json::json!({"query": "hi", "limit": "5"})),
        )
        .unwrap();
        assert_eq!(err, "Field 'limit' must be of type integer");
    }

    #[test]
    fn bool_is_not_an_integer_or_number() {
        let err = validate_input(
            &schema(),
            &args(serde_json::json!({"query": "hi", "limit": true})),
        )
        .unwrap();
        assert!(err.contains("limit"), "{err}");
        let err = validate_input(
            &schema(),
            &args(serde_json::json!({"query": "hi", "ratio": true})),
        )
        .unwrap();
        assert!(err.contains("ratio"), "{err}");
    }

    #[test]
    fn number_accepts_int_and_float() {
        assert_eq!(
            validate_input(
                &schema(),
                &args(serde_json::json!({"query": "hi", "ratio": 1}))
            ),
            None
        );
        assert_eq!(
            validate_input(
                &schema(),
                &args(serde_json::json!({"query": "hi", "ratio": 1.5}))
            ),
            None
        );
    }

    #[test]
    fn enum_out_of_range_uses_python_repr() {
        let err = validate_input(
            &schema(),
            &args(serde_json::json!({"query": "hi", "mode": "medium"})),
        )
        .unwrap();
        assert_eq!(err, "Field 'mode' must be one of ['fast', 'slow']");
    }

    #[test]
    fn unknown_fields_pass_through() {
        assert_eq!(
            validate_input(
                &schema(),
                &args(serde_json::json!({"query": "hi", "extra": 123}))
            ),
            None
        );
        // A schema without properties constrains nothing but required.
        let bare = serde_json::json!({"required": ["query"]});
        assert_eq!(
            validate_input(
                &bare,
                &args(serde_json::json!({"query": "hi", "extra": 123}))
            ),
            None
        );
    }

    #[test]
    fn envelopes_have_the_one_error_shape() {
        let body = validation_error("Missing required field: 'query'");
        assert_eq!(
            body,
            serde_json::json!({"error": {
                "code": "validation_error",
                "message": "Missing required field: 'query'",
                "details": null,
            }})
        );
        assert_eq!(
            unknown_tool("nope"),
            serde_json::json!({"error": {
                "code": "unknown_tool",
                "message": "Unknown tool: nope",
                "details": null,
            }})
        );
        assert_eq!(
            failed("test_pack.tool", "ordinary boom")["error"]["code"],
            "test_pack.tool_failed"
        );
    }

    #[test]
    fn denial_says_so_instead_of_looking_like_a_crash() {
        let body = permission_denied("test_pack", "llm");
        assert_eq!(body["error"]["code"], "permission_denied");
        assert_eq!(
            body["error"]["details"],
            serde_json::json!({"plugin": "test_pack", "permission": "llm"})
        );
        assert!(body["error"]["message"]
            .as_str()
            .unwrap()
            .contains("plugin.yaml"));
        assert_eq!(
            permission_denied_message("test_pack", "network"),
            "Plugin 'test_pack' lacks permission 'network'. \
             Approve it in the permissions section of plugin.yaml before use."
        );
    }
}
