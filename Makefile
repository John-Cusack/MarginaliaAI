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

COMPOSE := tools/dev-postgres/docker-compose.yml

# The port the dev container publishes. `tools/dev-postgres/docker-compose.yml`
# is the source of truth; `tests/unit/test_dev_environment.py` fails if the two
# drift, because a host check against the wrong port reports a healthy database
# as down and there is nothing in the message to say which happened.
DB_PORT := 5435

db: ## Start Postgres with pgvector
	docker compose -f $(COMPOSE) up -d
	@echo "Waiting for Postgres..."
	@until docker compose -f $(COMPOSE) exec -T postgres \
		pg_isready -U re_dev -d research_engine > /dev/null 2>&1; do sleep 1; done
	@echo "Postgres ready on localhost:$(DB_PORT)"

db-stop: ## Stop Postgres
	docker compose -f $(COMPOSE) down

db-status: ## Check DB status
	@pg_isready -U re_dev -d research_engine -h localhost -p $(DB_PORT) > /dev/null 2>&1 \
		&& echo "DB is up on localhost:$(DB_PORT)" \
		|| echo "DB is down (checked localhost:$(DB_PORT))"

ALEMBIC_INI := packages/core/src/research_engine/adapters/storage/postgres/migrations/alembic.ini

migrate: ## Upgrade the database to the packaged schema head
	uv run research-engine db upgrade

migrate-down: ## Revert the most recent migration
	uv run alembic -c $(ALEMBIC_INI) downgrade -1

migrate-status: ## Show the current database revision
	uv run research-engine db current

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
