SHELL:=/usr/bin/env bash # Use bash syntax, mitigates dash's printf on Debian
export TOP:=$(shell dirname "$(abspath $(lastword $(MAKEFILE_LIST)))")
name:=$(shell basename "$(TOP)")
export PIP_FIND_LINKS:=$(abspath $(TOP)/whl_local/)
export PYTHONPATH:=$(TOP)/src


.PHONY: help
help:
	@echo
	@echo "▍Help"
	@echo "▀▀▀▀▀▀"
	@echo
	@echo "Available targets:"
	@echo "    check:              run checks"
	@echo "    test:               run all tests"
	@echo "    coverage:           run all tests and collect code coverage"
	@echo "    lint:               run linters"
	@echo
	@echo "    format:             auto-format code with ruff"
	@echo
	@echo "    build:              build the source and whl package, look for */dist/*.whl"
	@echo
	@echo "    release:            tag a new release (required: V=X.Y.Z), e.g. make V=1.0.0 release"
	@echo
	@echo "    run:                sync dev environment and run the app (development mode)"
	@echo
	@echo "    clean:              clean the build tree"
	@echo
	@printf "Makefile debug: name=%q, PYTHONPATH=%q, PIP_FIND_LINKS=%q\n\n" "$(name)" "$(PYTHONPATH)" "$(PIP_FIND_LINKS)"


.PHONY: check
check: lint test


.PHONY: test
test:
	uv run pytest -v


.PHONY: coverage
coverage:
	uv run pytest -v --cov . --cov-report=term-missing


.PHONY: lint
lint:
	uv run ruff check src/ tests/
	uv run ruff format --check src/ tests/


.PHONY: format
format:
	uv run ruff format src/ tests/
	uv run ruff check --fix src/ tests/


.PHONY: build
build:
	uv build
	mkdir -p "$(PIP_FIND_LINKS)/"
	cp dist/*.whl "$(PIP_FIND_LINKS)/"


.PHONY: run
run:
	uv sync --group dev
	uv run usbguard-gui


RPM_VER ?= $(shell git tag --sort=-version:refname | grep -E '^v?[0-9]' | head -1 | sed 's/^v//')
RPM_REV ?= 0

.PHONY: rpmprep
rpmprep:
	@[ -n "$(RPM_VER)" ] || { echo "Error: RPM_VER could not be determined (no release tags found)."; exit 1; }
	cp "rpm/$(name).spec.in" "$(name).spec"
	sed -i 's|^Version:.*|Version:        $(RPM_VER)|g' "$(name).spec"
	sed -i 's|^Release:.*|Release:        $(RPM_REV)%{?dist}|g' "$(name).spec"
	rm -rf ~/rpmbuild/RPMS/noarch/"$(name)"*.rpm ~/rpmbuild/SRPMS/"$(name)"*.src.rpm
	uv build --sdist
	mkdir -p ~/rpmbuild/SOURCES
	cp dist/$(name)-$(RPM_VER).tar.gz ~/rpmbuild/SOURCES/


.PHONY: rpm
rpm: rpmprep
	rpmbuild -ba "$(name).spec"
	rm "$(name).spec"


.PHONY: srpm
srpm: rpmprep
	rpmbuild -bs "$(name).spec"
	rm "$(name).spec"


.PHONY: release
release:
	@[ -n "$(V)" ] || { echo "Error: V is not set.  Usage: make V=X.Y.Z release"; exit 1; }
	"$(TOP)/release.py" "$(V)"


.PHONY: clean
clean:
	-uv run coverage erase
	find "$(TOP)" -depth \( -name '__pycache__' -o -name '*.pyc' -o -name '*.pyo' -o -name '*.pyd' -o -name '*.egg-info' -o -name '*.py,cover' \) \
		-not -path '*/.git/*' -exec rm -rf {} +
	rm -rf "$(TOP)/build/" "$(TOP)/dist/" "$(TOP)/.tox/" \
		"$(TOP)/.pytest_cache/" "$(TOP)/.coverage"
