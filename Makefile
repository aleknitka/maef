.PHONY: api worker studio test
api:
	uvicorn maef_api.main:app --app-dir apps/api --reload --host 127.0.0.1 --port 8000
worker:
	cd apps/api && python -m maef_api.worker
studio:
	cd apps/studio && npm run dev
test:
	pytest -q
