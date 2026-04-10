SHELL:=/usr/bin/env bash # Use bash syntax, mitigates dash's printf on Debian
export TOP:=$(shell dirname "$(abspath $(lastword $(MAKEFILE_LIST)))")
name:=$(shell basename "$(TOP)")
export PIP_FIND_LINKS:=$(abspath $(TOP)/whl_local/)
export PYTHONPATH:=$(TOP)/src
RPM_VER ?= $(shell git tag --sort=-version:refname | grep -E '^v?[0-9]' | head -1 | sed 's/^v//')
RPM_REV ?= 0
outdir ?= dist


.PHONY: help
help:
	@echo
	@echo "▍Help"
	@echo "▀▀▀▀▀▀"
	@echo
	@echo "Available targets:"
	@echo "    lint:               run linters"
	@echo "    test:               run tests"
	@echo "    check:              test + lint"
	@echo "    coverage:           run tests and collect code coverage"
	@echo
	@echo "    format:             auto-format code with autopep8"
	@echo
	@echo "    run:                sync dev environment and run the app (development mode)"
	@echo
	@echo "    build:              build the source and whl package, look for */dist/*.whl"
	@echo "    srpm:               build source RPM package (optional: outdir=/path)"
	@echo "    rpm:                build RPM package"
	@echo "    rpmmock:            build RPM package using mock (recommended)"
	@echo
	@echo "    release:            tag a new release (required: V=X.Y.Z), e.g. make V=1.0.0 release"
	@echo
	@echo "    clean:              clean the build tree"
	@echo "    distclean (dc):     clean everything (even the virtual environment)"
	@echo
	@printf "Makefile debug info:\n\t- name=%q\n\t- PYTHONPATH=%q\n\t- PIP_FIND_LINKS=%q\n\n" \
		"$(name)" "$(PYTHONPATH)" "$(PIP_FIND_LINKS)"


.PHONY: lint
lint:
	uv run ruff check src/ tests/
	uv run autopep8 --check --recursive src/ tests/


.PHONY: test
test:
	uv run pytest -v


.PHONY: check
check: lint test


.PHONY: coverage
coverage:
	uv run pytest -v --cov . --cov-report=term-missing


.PHONY: format
format:
	uv run autopep8 --in-place --recursive src/ tests/
	uv run ruff check --fix src/ tests/


.PHONY: run
run:
	uv sync --group dev
	uv run python -m usbguard_gui


.PHONY: build
build:
	uv build
	mkdir -p "$(PIP_FIND_LINKS)/"
	cp dist/*.whl "$(PIP_FIND_LINKS)/"


.PHONY: rpmprep
rpmprep:
	@[ -n "$(RPM_VER)" ] || { echo "Error: RPM_VER could not be determined (no release tags found)."; exit 1; }
	uv build --sdist
	cp rpm/* dist/
	mv "dist/$(name).spec.in" "dist/$(name).spec"
	sed -i 's|^Version:.*|Version:        $(RPM_VER)|g'           "dist/$(name).spec"
	sed -i 's|^Release:.*|Release:        $(RPM_REV)%{?dist}|g'   "dist/$(name).spec"
	rpmbuild --define "_sourcedir $(TOP)/dist" --define "_srcrpmdir $(TOP)/dist" --define "_rpmdir $(TOP)/dist" \
		-bs "dist/$(name).spec"


.PHONY: srpm
srpm: rpmprep
	rm -f "$(outdir)"/*.rpm
	rpmbuild --define "_sourcedir $(TOP)/dist" --define "_srcrpmdir $(TOP)/$(outdir)" \
		-bs "dist/$(name).spec"


.PHONY: rpm
rpm: srpm
	rpmbuild --define "_rpmdir $(TOP)/dist" --rebuild dist/*.src.rpm
	mv dist/noarch/* dist/
	rmdir dist/noarch


.PHONY: rpmmock
rpmmock: srpm
	mock --rebuild dist/*.src.rpm --resultdir=dist/ # --no-cleanup-after


.PHONY: release
release:
	@[ -n "$(V)" ] || { echo "Error: V is not set.  Usage: make V=X.Y.Z release"; exit 1; }
	@uv run python3 -u release.py "$(V)"


.PHONY: clean
clean:
	-uv run coverage erase
	rm -f .coverage
	rm -rf dist/*
	find . -depth -type d \( -name '__pycache__' -o -name '*.egg-info' -o -name '*.dist-info' \) \
		-not -path './.git/*' -not -path './.venv/*' \
		-exec rm -rf {} +
	find . -depth -type f \( -name '*.pyc' -o -name '*.pyo' -o -name '*.pyd' -o -name '*.py,cover' \) \
		-not -path './.git/*' -not -path './.venv/*' \
		-exec rm -f {} +


.PHONY: distclean
distclean: clean
	rm -rf whl_local/* .venv .ruff_cache dist .tox build .pytest_cache uv.lock


.PHONY: dc
dc: distclean
