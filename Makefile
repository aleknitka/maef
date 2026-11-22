.PHONY: api worker studio test test-oci
api:
	uvicorn maef_api.main:app --app-dir apps/api --reload --host 127.0.0.1 --port 8000
worker:
	cd apps/api && python -m maef_api.worker
studio:
	cd apps/studio && npm run dev
test:
	pytest -q
test-oci:
	MAEF_ALLOW_UNPINNED_IMAGES=1 pytest -q -m oci tests/test_sandbox_oci.py
