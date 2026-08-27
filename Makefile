# MarginaliaAI — Development helpers
# ====================================
#
# GPU vs CPU:
#   Docling (PDF parsing):    CPU parallel for large PDFs. RE_DOCLING_DEVICE=auto for GPU on small PDFs.
#                             RE_DOCLING_MAX_WORKERS / RE_DOCLING_PAGES_PER_TASK cap memory use;
#                             unset sizes them from this machine's cores and free RAM.
#   Embedding (bge-m3):       GPU auto-detected by sentence-transformers. Strongly benefits from CUDA.
#   Reranking (bge-reranker): GPU auto-detected. Strongly benefits from CUDA.

.PHONY: db db-stop db-status migrate migrate-down migrate-status test test-integration test-all lint help

db: ## Start Postgres with pgvector
	docker compose -f tools/dev-postgres/docker-compose.yml up -d
	@echo "Waiting for Postgres..."
	@until docker compose -f tools/dev-postgres/docker-compose.yml exec -T postgres \
		pg_isready -U re_dev -d research_engine > /dev/null 2>&1; do sleep 1; done
	@echo "Postgres ready on localhost:5432"

db-stop: ## Stop Postgres
	docker compose -f tools/dev-postgres/docker-compose.yml down

db-status: ## Check DB status
	@pg_isready -U re_dev -d research_engine -h localhost 2>/dev/null \
		&& echo "DB is up" || echo "DB is down"

ALEMBIC_INI := packages/core/src/research_engine/adapters/storage/postgres/migrations/alembic.ini

migrate: ## Run Alembic migrations
	uv run alembic -c $(ALEMBIC_INI) upgrade head

migrate-down: ## Revert the most recent migration
	uv run alembic -c $(ALEMBIC_INI) downgrade -1

migrate-status: ## Show current and available revisions
	uv run alembic -c $(ALEMBIC_INI) current
	uv run alembic -c $(ALEMBIC_INI) history

test: ## Run unit tests
	uv run pytest tests/unit/ -v

test-integration: ## Run integration tests (needs `make db`; skips without one)
	uv run pytest tests/integration/ -v

test-all: ## Run every test
	uv run pytest tests/ -v

lint: ## Lint
	uv run ruff check packages/ tests/

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-12s\033[0m %s\n", $$1, $$2}'
