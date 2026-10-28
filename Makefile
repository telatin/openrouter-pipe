# Rebuild the single-file bundles. Run `make help` for targets.
# Everything runs inside the micromamba env MAMBA_ENV (default webui-dev).

MAMBA_ENV ?= webui-dev
RUN       ?= micromamba run -n $(MAMBA_ENV)
PYTHON    ?= $(RUN) python

FLAT            = open_webui_openrouter_pipe_bundled.py
COMPRESSED      = open_webui_openrouter_pipe_bundled_compressed.py
FLAT_NP         = open_webui_openrouter_pipe_bundled_no_plugins.py
COMPRESSED_NP   = open_webui_openrouter_pipe_bundled_compressed_no_plugins.py
BUNDLES         = $(FLAT) $(COMPRESSED) $(FLAT_NP) $(COMPRESSED_NP)

SOURCES = $(shell find open_webui_openrouter_pipe -name '*.py' -not -path '*/__pycache__/*') \
          open_webui_openrouter_pipe.py scripts/bundle_v2.py scripts/anyio_1111_workaround.py

.PHONY: help bundle all clean test lint gate release release-dry-run

help:
	@echo "make bundle      flat bundle only ($(FLAT))"
	@echo "make all         all four bundles (rebuilt only when a source file changed)"
	@echo "make clean       delete the four bundles"
	@echo "make test        package test suite"
	@echo "make lint        ruff (source) + pyright"
	@echo "make release    create a GitHub release v<version> with gh and upload the four bundles"
	@echo "make release-dry-run  run the release checks and print the gh command only"
	@echo "make gate        scripts/gate.sh batch: bundles, package tests, ruff, pyright (~3 min)"

bundle: $(FLAT)

all: $(BUNDLES)

$(FLAT): $(SOURCES)
	$(PYTHON) scripts/bundle_v2.py

$(COMPRESSED): $(SOURCES)
	$(PYTHON) scripts/bundle_v2.py --compress

$(FLAT_NP): $(SOURCES)
	$(PYTHON) scripts/bundle_v2.py --no-plugins

$(COMPRESSED_NP): $(SOURCES)
	$(PYTHON) scripts/bundle_v2.py --compress --no-plugins

clean:
	rm -f $(BUNDLES)

test:
	PYTHONPATH=. $(PYTHON) -m pytest tests -q -p xdist -n 4 --dist loadfile

lint:
	$(RUN) ruff check --extend-ignore EXE002 open_webui_openrouter_pipe/ scripts/ filters/
	$(RUN) pyright

gate:
	bash scripts/gate.sh batch

release:
	bash scripts/release.sh

release-dry-run:
	DRY_RUN=1 bash scripts/release.sh
