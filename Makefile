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
# -T: no TTY, so stdin/stdout carry the file bytes unmodified. Files cross the host/volume
# boundary by streaming through a container running as the service user (uid 10001), so
# they are created with the right owner and follow the operator's umask on the host.
SHT = docker compose run --rm -T --entrypoint sh
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
# there, streamed out to ./backups/<name>.part, renamed to its final name only when the
# stream succeeded, and only then is the in-volume staging copy removed (a full volume
# breaks database writes). A failed copy-out keeps the staging file and never leaves a
# file that looks like a finished backup. No existing service container is needed.
# Only the file NAME of BACKUP_FILE is honoured: the copy always lands in ./backups.
# An existing ./backups/<name> is never overwritten unless FORCE=1.
# --force is always passed: a leftover staging copy of the same name in the volume is
# overwritten even without FORCE=1; FORCE=1 only guards the file on the host.
# Default name has seconds: upb-<date>_<hhmmss>.sqlite3.
BACKUP_NAME := $(if $(BACKUP_FILE),$(notdir $(BACKUP_FILE)),upb-$(shell date +%F_%H%M%S).sqlite3)
backup: _need-version ## Snapshot, verify, stream to ./backups (BACKUP_FILE=<name> names it; refuses to overwrite unless FORCE=1)
	@if [ -e '$(BACKUP_DIR)/$(BACKUP_NAME)' ] && [ "$(FORCE)" != 1 ]; then \
	  echo "$(BACKUP_DIR)/$(BACKUP_NAME) already exists: pick another BACKUP_FILE or pass FORCE=1 to overwrite" >&2; exit 2; fi
	@mkdir -p $(BACKUP_DIR)
	@# chmod kept on purpose: it also normalises a 0777 directory left by an older Makefile.
	@chmod 0750 $(BACKUP_DIR)
	$(CLI) $(SERVICE) -m app.cli backup --force $(VOLUME_DIR)/$(BACKUP_NAME)
	$(CLI) $(SERVICE) -m app.cli verify $(VOLUME_DIR)/$(BACKUP_NAME)
	$(SHT) $(SERVICE) -c 'cat $(VOLUME_DIR)/$(BACKUP_NAME)' > $(BACKUP_DIR)/$(BACKUP_NAME).part
	mv $(BACKUP_DIR)/$(BACKUP_NAME).part $(BACKUP_DIR)/$(BACKUP_NAME)
	$(SH) $(SERVICE) -c 'rm -f $(VOLUME_DIR)/$(BACKUP_NAME)'

# The file is streamed into the volume first, so host directory permissions do not matter.
# The in-volume staging copy is removed afterwards whatever the result; the data stays on the host.
verify: _need-version ## Check a backup file: make verify FILE=backups/<file> (streamed into the volume, removed after)
	@test -n "$(FILE)" || { echo "usage: make verify FILE=backups/<file>" >&2; exit 2; }
	$(SH) $(SERVICE) -c 'mkdir -p $(VOLUME_DIR)'
	rc=0; { $(SHT) $(SERVICE) -c 'cat > $(VOLUME_DIR)/$(notdir $(FILE))' < '$(FILE)' \
	  && $(CLI) $(SERVICE) -m app.cli verify $(VOLUME_DIR)/$(notdir $(FILE)); } || rc=$$?; \
	if $(SH) $(SERVICE) -c 'rm -f $(VOLUME_DIR)/$(notdir $(FILE))'; then \
	  test $$rc -eq 0 || echo "verify failed (exit $$rc); staging copy removed from the volume" >&2; \
	else \
	  echo "could not remove staging copy $(VOLUME_DIR)/$(notdir $(FILE)) from the volume" >&2; \
	  test $$rc -ne 0 || rc=1; \
	fi; exit $$rc

.PHONY: _need-version
_need-version:
	@test -n "$(APP_VERSION)" || { echo "APP_VERSION is not set: add it to .env or pass APP_VERSION=..." >&2; exit 2; }
