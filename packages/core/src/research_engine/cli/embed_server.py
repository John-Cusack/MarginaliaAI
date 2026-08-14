"""`research-engine embed-server` — lend this machine's GPU to the engine."""

from __future__ import annotations

import typer

embed_server_app = typer.Typer(no_args_is_help=False)


@embed_server_app.callback(invoke_without_command=True)
def serve(
    ctx: typer.Context,
    host: str = typer.Option("0.0.0.0", "--host", help="Bind address."),  # noqa: S104
    port: int = typer.Option(9882, "--port", "-p", help="Bind port."),
    model: str = typer.Option(
        None, "--model", help="Model to serve. Defaults to RE_EMBEDDING_MODEL."
    ),
    dim: int = typer.Option(0, "--dim", help="Vector width. Defaults to RE_EMBEDDING_DIM."),
    device: str = typer.Option(None, "--device", help="Torch device override."),
    warm: bool = typer.Option(
        True, "--warm/--lazy", help="Load the model at startup rather than on first use."
    ),
    concurrency: int = typer.Option(
        1,
        "--concurrency",
        "-c",
        help="Batches allowed on the GPU at once. 1 is safest; 2 suits a 24GB card.",
    ),
) -> None:
    """Serve embeddings over HTTP for another machine to use.

    Run this on the host with the fast card, then point the engine at it:

        RE_EMBEDDING_PROVIDER=remote_api
        RE_EMBEDDING_BASE_URL=http://<this-host>:9882

    The model served here must match the corpus's model exactly. It is reported
    on /health and checked on every request, because vectors from a different
    model are not comparable with the ones already stored and nothing in the
    database would catch it.
    """
    if ctx.invoked_subcommand is not None:
        return

    import uvicorn

    from research_engine.adapters.embedding.server import create_app
    from research_engine.config import load_settings

    settings = load_settings()
    app = create_app(
        model or settings.embedding_model,
        dim or settings.embedding_dim,
        device=device,
        warm=warm,
        concurrency=concurrency,
    )

    typer.echo(
        f"Serving {model or settings.embedding_model} "
        f"(dim {dim or settings.embedding_dim}) on {host}:{port}, "
        f"concurrency {concurrency}"
    )
    uvicorn.run(app, host=host, port=port, log_level="info")
