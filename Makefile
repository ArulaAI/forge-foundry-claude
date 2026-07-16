# forge-foundry-claude — maintenance tasks.
#
# The plugin is produced by re-vendoring skills + the forge_sync engine from a
# pinned tag of forge-foundry-primitives. `make vendor` provisions a throwaway
# environment with the emitter's runtime deps and runs scripts/vendor.py — so a
# re-vendor "just works" from any shell, without depending on the source library's
# venv (which shadows the plugin's `scripts` package — see scripts/vendor.py).
#
# Usage:
#   make vendor SOURCE=/path/to/forge-foundry-primitives REF=v8.5.1 VERSION=0.2.0
#   make test
#   make lint

# The emitter API that copy_agents imports at vendor time (forge_sync.emitter.*)
# needs these at runtime. They are NOT plugin runtime deps — only vendor-time.
VENDOR_DEPS := pyyaml pydantic tomli-w jinja2 click defusedxml sqlglot

VERSION ?= 0.2.0

.PHONY: vendor test lint help

help:
	@echo "make vendor SOURCE=<primitives-path> REF=<tag> [VERSION=$(VERSION)]  — re-vendor the plugin"
	@echo "make test                                                            — run the parity + smoke tests"
	@echo "make lint                                                            — ruff check (engine/ + skills/ excluded)"

vendor:
	@test -n "$(SOURCE)" || (echo "error: SOURCE=<path to forge-foundry-primitives> is required" && exit 2)
	@test -n "$(REF)"    || (echo "error: REF=<git tag/ref of the source> is required" && exit 2)
	@echo "Re-vendoring from $(SOURCE)@$(REF) → plugin $(VERSION)"
	uv run --no-project $(addprefix --with ,$(VENDOR_DEPS)) \
		python scripts/vendor.py --source "$(SOURCE)" --ref "$(REF)" --version "$(VERSION)" $(VENDOR_ARGS)

test:
	uv run --no-project --with pytest $(addprefix --with ,$(VENDOR_DEPS)) \
		python -m pytest tests/ -q

lint:
	uv run --no-project --with ruff ruff check .
