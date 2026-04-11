export TOP:=$(shell dirname "$(abspath $(lastword $(MAKEFILE_LIST)))")
outdir ?= dist

.PHONY: srpm
srpm:
	$(MAKE) -C $(TOP)/.. srpm outdir=$(outdir)
