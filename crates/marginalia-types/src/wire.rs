//! Wire helpers for JSON-boundary fidelity with the Pydantic models.
//!
//! `bytes` fields (`content_hash`, …) are Postgres `bytea` in storage, but on
//! the JSON boundary Pydantic renders them as UTF-8 strings — and refuses to
//! render non-UTF-8 bytes at all (`PydanticSerializationError`). The helpers
//! here encode the same contract: strings on the wire, loud failure for
//! non-UTF-8 payloads, so a real sha256 hash can never silently cross as JSON
//! on either side.

/// `Vec<u8>` that serializes as a UTF-8 string, like Pydantic's JSON mode.
pub mod bytes_string {
    use serde::ser::Error as _;
    use serde::{Deserialize, Deserializer, Serialize, Serializer};

    pub fn serialize<S: Serializer>(value: &[u8], serializer: S) -> Result<S::Ok, S::Error> {
        match std::str::from_utf8(value) {
            Ok(s) => s.serialize(serializer),
            Err(_) => Err(S::Error::custom(
                "bytes are not UTF-8 representable as JSON string",
            )),
        }
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(deserializer: D) -> Result<Vec<u8>, D::Error> {
        String::deserialize(deserializer).map(String::into_bytes)
    }
}

/// `Option<Vec<u8>>` variant of [`bytes_string`].
pub mod bytes_string_opt {
    use serde::ser::Error as _;
    use serde::{Deserialize, Deserializer, Serializer};

    pub fn serialize<S: Serializer>(
        value: &Option<Vec<u8>>,
        serializer: S,
    ) -> Result<S::Ok, S::Error> {
        match value {
            Some(bytes) => match std::str::from_utf8(bytes) {
                Ok(s) => serializer.serialize_some(s),
                Err(_) => Err(S::Error::custom(
                    "bytes are not UTF-8 representable as JSON string",
                )),
            },
            None => serializer.serialize_none(),
        }
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(
        deserializer: D,
    ) -> Result<Option<Vec<u8>>, D::Error> {
        Option::<String>::deserialize(deserializer).map(|o| o.map(String::into_bytes))
    }
}
