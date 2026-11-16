PYTHON ?= python3
SETUP_ARGS ?=
export MIRROR_UID := $(shell id -u)
export MIRROR_GID := $(shell id -g)

.PHONY: up down prepare test
prepare:
	$(PYTHON) deploy/setup.py $(SETUP_ARGS)
up: prepare
	docker compose --env-file runtime/compose.env up --build -d
down:
	docker compose --env-file runtime/compose.env down
test:
	$(PYTHON) -m unittest discover -s tests -v
