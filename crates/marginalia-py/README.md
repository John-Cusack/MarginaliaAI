# marginalia-ai-accelerator

Optional prebuilt Rust accelerator for `marginalia-ai`. Installing it makes
`RE_RUST_BACKEND=auto` (the default) route hot pure paths through the
`marginalia_rs` native extension. Without it, everything runs on the
pure-Python implementation — no Rust toolchain is ever required to install
or use `marginalia-ai`.

```bash
pip install 'marginalia-ai[accelerated]'
```

Accelerated paths, each 1.5x–8x faster than pure Python, including the cost
of crossing into Rust: quote normalization (`normalize`,
`normalize_for_matching`, `normalize_with_map`), prose and structural
chunking, and markdown parsing. Everything else stays Python, because a
native call there costs more than it saves.

The Rust answers are byte-identical to Python's only when both use the same
Unicode tables. This wheel is built for Unicode 15.1, which is Python 3.13's,
so `RE_RUST_BACKEND=auto` uses it on Python 3.13 and quietly stays on the
pure-Python path on 3.11, 3.12 and 3.14.

`RE_RUST_BACKEND=python` forces the pure-Python path (bisection/rollback);
`RE_RUST_BACKEND=rust` forces the native path and fails loudly when this
package is absent.
