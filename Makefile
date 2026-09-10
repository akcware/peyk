.PHONY: db migrate run-gateway run-workers run-sessions test gate-0 status lint requeue-failed

db:
	docker compose up -d db

migrate: db
	uv run python -m db.migrate

run-gateway:
	uv run uvicorn gateway.app:app --reload --port 8000

run-workers:
	uv run python -m workers.main

run-sessions:
	uv run python -m sessions.main

lint:
	uv run ruff check .

# Test DB: separate database on the same compose postgres.
test-db: db
	docker compose exec -T db psql -U agent -d agent -tc "SELECT 1 FROM pg_database WHERE datname='agent_test'" | grep -q 1 || docker compose exec -T db psql -U agent -d agent -c "CREATE DATABASE agent_test"

test: test-db
	uv run pytest -q -m "not llm"

gate-0: test-db
	uv run pytest -q tests/phase0

status:
	docker compose exec -T db psql -U agent -d agent -c "select source, status, count(*) from observation group by 1,2 order by 1,2"

# Put failed observations back on the queue (after fixing the cause).
requeue-failed:
	docker compose exec -T db psql -U agent -d agent -c "update observation set status='new', attempts=0, claimed_at=null where status='failed'"
