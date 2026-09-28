# Developer and operator shortcuts. Run `make` for the list.

SHELL := /bin/bash
.DEFAULT_GOAL := help

PYTHON  ?= python3
VENV    ?= .venv
SERVICE := users-ping-bot
IMAGE   := users-ping-bot
# Image tag: taken from APP_VERSION in .env unless given on the command line.
APP_VERSION ?= $(shell sed -n 's/^APP_VERSION=//p' .env 2>/dev/null)

CLI = docker compose run --rm --entrypoint python
SH  = docker compose run --rm --entrypoint sh
BACKUP_DIR := backups
VOLUME_DIR := /data/backups

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

# The snapshot is written inside the data volume (where uid 10001 has rights), verified
# there, copied out, and only then is the in-volume staging copy removed (a full volume
# breaks database writes). A failed copy-out keeps the staging file. Needs the service
# container to exist (`make up` once). Only the file NAME of BACKUP_FILE is honoured:
# the copy always lands in ./backups. Default name has seconds: upb-<date>_<hhmmss>.sqlite3.
BACKUP_NAME := $(if $(BACKUP_FILE),$(notdir $(BACKUP_FILE)),upb-$(shell date +%F_%H%M%S).sqlite3)
backup: _need-version ## Snapshot into the volume, verify it there, copy to ./backups (BACKUP_FILE=<name> sets the name only)
	@mkdir -p $(BACKUP_DIR)
	@chmod 0750 $(BACKUP_DIR)
	$(CLI) $(SERVICE) -m app.cli backup $(VOLUME_DIR)/$(BACKUP_NAME)
	$(CLI) $(SERVICE) -m app.cli verify $(VOLUME_DIR)/$(BACKUP_NAME)
	docker compose cp $(SERVICE):$(VOLUME_DIR)/$(BACKUP_NAME) $(BACKUP_DIR)/$(BACKUP_NAME)
	$(SH) $(SERVICE) -c 'rm -f $(VOLUME_DIR)/$(BACKUP_NAME)'

# The file is copied into the volume first, so host directory permissions do not matter.
# The copy stays in the volume; remove it when no longer needed.
verify: _need-version ## Check a backup file: make verify FILE=backups/<file> (copied into the volume first)
	@test -n "$(FILE)" || { echo "usage: make verify FILE=backups/<file>" >&2; exit 2; }
	$(SH) $(SERVICE) -c 'mkdir -p $(VOLUME_DIR)'
	docker compose cp $(FILE) $(SERVICE):$(VOLUME_DIR)/$(notdir $(FILE))
	$(CLI) $(SERVICE) -m app.cli verify $(VOLUME_DIR)/$(notdir $(FILE))

.PHONY: _need-version
_need-version:
	@test -n "$(APP_VERSION)" || { echo "APP_VERSION is not set: add it to .env or pass APP_VERSION=..." >&2; exit 2; }
