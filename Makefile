# Developer and operator shortcuts. Run `make` for the list.

SHELL := /bin/bash
.DEFAULT_GOAL := help

PYTHON  ?= python3
VENV    ?= .venv
SERVICE := ping-pong-bot
IMAGE   := ping-pong-bot
# Image tag: taken from APP_VERSION in .env unless given on the command line.
APP_VERSION ?= $(shell sed -n 's/^APP_VERSION=//p' .env 2>/dev/null)

CLI = docker compose run --rm --entrypoint python
BACKUP_DIR := backups
BACKUP_FILE ?= $(BACKUP_DIR)/upb-$(shell date +%F_%H%M).sqlite3

.PHONY: help venv test build config up stop restart logs ps set-root backup verify

help: ## Show this list
	@grep -hE '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

venv: $(VENV)/bin/pytest ## Create the virtualenv with runtime and test dependencies

$(VENV)/bin/pytest: requirements.txt requirements-dev.txt
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install -q -r requirements-dev.txt
	@touch $@

test: venv ## Run the test suite (ARGS=... passes extra pytest arguments)
	$(VENV)/bin/pytest -q $(ARGS)

build: _need-version ## Build the image tagged with APP_VERSION
	docker build -t $(IMAGE):$(APP_VERSION) .

config: ## Validate docker-compose.yml against .env
	docker compose config -q

up: _need-version ## Start the bot in the background
	docker compose up -d

stop: ## Stop the bot, keeping the container and data
	docker compose stop

restart: ## Restart the bot
	docker compose restart

logs: ## Follow the bot logs
	docker compose logs -f --tail=100

ps: ## Show the container state
	docker compose ps

set-root: _need-version ## Assign root: make set-root ID=<telegram_user_id>
	@test -n "$(ID)" || { echo "usage: make set-root ID=<telegram_user_id>" >&2; exit 2; }
	$(CLI) $(SERVICE) -m app.cli set-root $(ID)

# The container runs as uid 10001, so the host backup directory must be writable for it.
backup: _need-version ## Snapshot the live database into ./backups and verify the copy
	@mkdir -p $(BACKUP_DIR) && chmod a+rwx $(BACKUP_DIR)
	$(CLI) -v "$(CURDIR)/$(BACKUP_DIR):/backups" $(SERVICE) -m app.cli backup /backups/$(notdir $(BACKUP_FILE))
	$(CLI) -v "$(CURDIR)/$(BACKUP_DIR):/backups" $(SERVICE) -m app.cli verify /backups/$(notdir $(BACKUP_FILE))

verify: _need-version ## Check a backup file: make verify FILE=backups/<file>
	@test -n "$(FILE)" || { echo "usage: make verify FILE=backups/<file>" >&2; exit 2; }
	$(CLI) -v "$(abspath $(dir $(FILE))):/backups:ro" $(SERVICE) -m app.cli verify /backups/$(notdir $(FILE))

.PHONY: _need-version
_need-version:
	@test -n "$(APP_VERSION)" || { echo "APP_VERSION is not set: add it to .env or pass APP_VERSION=..." >&2; exit 2; }
