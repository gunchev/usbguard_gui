export TOP:=$(shell dirname "$(abspath $(lastword $(MAKEFILE_LIST)))")
outdir ?= dist

.PHONY: srpm
srpm:
	dnf -y install uv python3 python3-build python3-hatchling
	$(MAKE) -C $(TOP)/.. srpm
	if test -n "$(outdir)"; then cp -a $(TOP)/../dist/*.src.rpm "$(outdir)"; fi
