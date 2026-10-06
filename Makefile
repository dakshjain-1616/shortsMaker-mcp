.PHONY: install lint test e2e run docker-build docker-up docker-down

install:
	python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt

lint:
	.venv/bin/ruff check .

test:
	.venv/bin/pytest -q

e2e:
	.venv/bin/pytest -q tests/e2e

run:
	.venv/bin/shortsmaker-mcp

docker-build:
	docker build -t shortsmaker-mcp:0.2.0 .

docker-up:
	docker compose up -d --build

docker-down:
	docker compose down
