.PHONY: up down test migrate

up:
	docker compose up --build

down:
	docker compose down

migrate:
	cd backend && alembic upgrade head

test:
	cd backend && python -m pytest

