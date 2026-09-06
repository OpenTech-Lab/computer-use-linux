.PHONY: setup test test-unit test-integration lint doctor

setup:
	bash scripts/bootstrap.sh
	UV_CACHE_DIR=/tmp/cul-uv-cache VIRTUAL_ENV=.venv uv pip install --python .venv/bin/python -e .

test:
	.venv/bin/pytest

test-unit:
	.venv/bin/pytest tests/unit

test-integration:
	.venv/bin/pytest tests/integration -m requires_session

lint:
	.venv/bin/python -m ruff check src tests

doctor:
	.venv/bin/cul doctor
