.PHONY: help db-up db-down backend-install backend frontend-install frontend dev test

# Resolve a REAL uv binary. On a pyenv box the PATH `uv` is a cwd-dependent shim that can
# point at a python version lacking uv; skip it and use a pyenv-version uv (a real binary
# that runs from any dir and manages its own Python). A non-shim PATH uv is preferred.
UV ?= $(shell u=$$(command -v uv 2>/dev/null); case "$$u" in */.pyenv/shims/*) u= ;; esac; if [ -n "$$u" ]; then echo $$u; else ls $(HOME)/.pyenv/versions/*/bin/uv 2>/dev/null | head -1; fi)

help:
	@echo "Conductor — personal Kanban cockpit"
	@echo ""
	@echo "  make db-up            start Postgres (docker)"
	@echo "  make db-down          stop Postgres"
	@echo "  make backend-install  create venv + install backend deps (uv, py3.13)"
	@echo "  make backend          run FastAPI (uvicorn, :8787)"
	@echo "  make frontend-install npm install"
	@echo "  make frontend         run Vite dev server (:5173)"
	@echo "  make test             backend tests (sqlite)"
	@echo ""
	@echo "Typical first run: make db-up backend-install frontend-install"
	@echo "Then in two terminals: 'make backend' and 'make frontend'"

db-up:
	docker compose up -d postgres

db-down:
	docker compose down

backend-install:
	cd backend && $(UV) sync --extra dev

backend:
	cd backend && $(UV) run uvicorn conductor.main:app --reload --host 127.0.0.1 --port 8787

frontend-install:
	cd frontend && npm install

frontend:
	cd frontend && npm run dev

test:
	cd backend && $(UV) run pytest -q
