.PHONY: api worker studio tui test test-oci test-pg test-s3 check
api:
	uv run uvicorn maef_api.main:app --app-dir apps/api --reload --host 127.0.0.1 --port 8000
worker:
	cd apps/api && uv run python -m maef_api.worker
studio:
	cd apps/studio && npm run dev
tui:
	cd apps/tui && bun run start
test:
	uv run pytest -q
test-oci:
	MAEF_ALLOW_UNPINNED_IMAGES=1 uv run pytest -q -m oci tests/test_sandbox_oci.py
test-pg:
	uv run pytest -q -m pg
test-s3:
	uv run pytest -q -m s3
check:
	uv run ruff check
	uv run ruff format --check
	uv run ty check
	uv run pytest -q
