PY := .venv/bin/python
PIP := uv pip install --python $(PY)

.PHONY: help setup up down db-check check-keys twin twin-reset journey chat test test-twin test-journey test-agent test-safety scenarios scenarios-check stt-eval

help:
	@echo "make setup       create .venv (Python 3.12) and install everything"
	@echo "make up / down   start / stop Postgres+pgvector in Docker"
	@echo "make db-check    confirm Postgres is healthy and pgvector loads"
	@echo "make check-keys  one tiny call per service, prints OK / FAIL"
	@echo "make twin        run the Hospital Twin on http://localhost:8001 (docs at /docs)"
	@echo "make twin-reset  wipe and reseed the twin database"
	@echo "make journey     run the Journey Engine API on http://localhost:8002/docs (needs make twin)"
	@echo "make chat        talk to Sahay by text (PHONE=+919845012345 to call as Ramesh; needs ANTHROPIC_API_KEY)"
	@echo "make scenarios   text scenario suite, 3 runs each (needs ANTHROPIC_API_KEY)"
	@echo "make test        run all tests"

setup:
	@test -d .venv || uv venv --python 3.12 .venv
	$(PIP) -r requirements.lock
	@test -f .env || (cp .env.example .env && echo "Created .env - fill in your keys")

up:
	docker compose up -d --wait

down:
	docker compose down

db-check:
	$(PY) scripts/check_keys.py --only postgres

check-keys:
	$(PY) scripts/check_keys.py

twin:
	$(PY) -m uvicorn twin.app:app --port 8001 --reload --reload-dir twin --reload-dir core

twin-reset:
	$(PY) -m twin.seed --reset

journey:
	$(PY) -m uvicorn journey.api:create_app --factory --port 8002

chat:
	$(PY) -m agent.textchat $(if $(PHONE),--phone $(PHONE),) $(if $(LANG_),--lang $(LANG_),)

test:
	$(PY) -m pytest

test-twin:
	$(PY) -m pytest tests/twin

test-journey:
	$(PY) -m pytest tests/journey

test-agent:
	$(PY) -m pytest tests/agent tests/eval

test-safety:
	$(PY) -m pytest tests/safety

scenarios:
	$(PY) -m eval.run --mode text

scenarios-check:
	$(PY) -m eval.run --check

stt-eval:
	@echo "Arrives in Stage 4 (needs SARVAM_API_KEY and recordings in eval/audio/)"
