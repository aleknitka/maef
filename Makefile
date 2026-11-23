.PHONY: api worker studio tui test test-oci test-pg test-s3
api:
	uvicorn maef_api.main:app --app-dir apps/api --reload --host 127.0.0.1 --port 8000
worker:
	cd apps/api && python -m maef_api.worker
studio:
	cd apps/studio && npm run dev
tui:
	cd apps/tui && bun run start
test:
	pytest -q
test-oci:
	MAEF_ALLOW_UNPINNED_IMAGES=1 pytest -q -m oci tests/test_sandbox_oci.py
test-pg:
	pytest -q -m pg
test-s3:
	pytest -q -m s3
