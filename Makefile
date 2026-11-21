.PHONY: api studio test
api:
	uvicorn maef_api.main:app --app-dir apps/api --reload --host 127.0.0.1 --port 8000
studio:
	cd apps/studio && npm run dev
test:
	pytest -q
