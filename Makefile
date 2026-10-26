.PHONY: bootstrap app app-clean app-alias install reload preflight install-plists tcc-reset onboard post-onboard-kick

ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
PYTHON := $(ROOT)/.venv/bin/python
# The installed command (install.sh / Homebrew), not the repo build venv: plists and restarts must
# target the same penpal the user runs. Override with `make reload PENPAL=/path/to/penpal`.
PENPAL ?= $(HOME)/.local/bin/penpal
BOOTSTRAP_PYTHON := python3
APP_DEST := /Applications/Penpal.app
PLIST_DST := $(HOME)/Library/LaunchAgents

bootstrap:
	rm -rf $(ROOT)/.venv
	$(BOOTSTRAP_PYTHON) -m venv $(ROOT)/.venv
	$(PYTHON) -m pip install --no-user --upgrade pip
	$(PYTHON) -m pip install --no-user -e '.[macos,build]'

app-alias:
	cd /tmp && $(PYTHON) $(ROOT)/setup_app.py py2app -A -d $(ROOT)/dist -b $(ROOT)/build

app:
	rm -rf build dist
	cd /tmp && $(PYTHON) $(ROOT)/setup_app.py py2app -d $(ROOT)/dist -b $(ROOT)/build

app-clean:
	rm -rf build dist

# preflight: verify built bundle has required Info.plist keys + pyobjc frameworks.
# Aborts install if anything is missing — prevents shipping a silently-broken bundle.
preflight:
	$(PYTHON) $(ROOT)/scripts/preflight_app.py $(ROOT)/dist/Penpal.app

# install-plists: write the launchd plists with the CLI itself (`penpal install launchd`), so every
# install path runs the `penpal` console script. No static plist files to drift (D33).
# --no-load: the `reload` target loads them. --force: they are Penpal-managed, regenerate on change.
install-plists:
	@mkdir -p $(PLIST_DST)
	PENPAL_EXECUTABLE=$(PENPAL) $(PENPAL) install launchd --no-load --force

# install: build .app, preflight, copy to /Applications, sync plists, reload all agents.
# Always cleans dist/Penpal.app at the end — leaving it would create a duplicate
# entry in System Settings → Accessibility (same bundle id, different path) and
# users grant permission to the wrong binary. See docs/incident-2026-05-17-selfheal-loop.md.
install: app preflight
	rm -rf $(APP_DEST)
	cp -R $(ROOT)/dist/Penpal.app $(APP_DEST)
	rm -rf $(ROOT)/dist/Penpal.app
	$(MAKE) install-plists
	$(MAKE) tcc-reset
	$(MAKE) reload
	$(MAKE) onboard
	$(MAKE) post-onboard-kick

# tcc-reset: clear TCC entries that get invalidated by bundle re-signing.
# Without this, macOS keeps the old csreq (cdhash) record. User toggling the
# Settings switch alone does NOT refresh csreq — the only ways to refresh are
# (a) tccutil reset + native prompt, or (b) manually removing the entry from
# the Settings list with "-" and re-adding it. We choose (a) here.
# AppleEvents (browser automation) is NOT reset: it stays valid across repacks,
# and resetting forces the user to re-grant Chrome control.
tcc-reset:
	@echo "--- tcc reset (Accessibility / Input Monitoring / Screen Recording) ---"
	-@tccutil reset Accessibility com.penpal.app 2>/dev/null && echo "  reset Accessibility" || true
	-@tccutil reset ListenEvent com.penpal.app 2>/dev/null && echo "  reset ListenEvent" || true
	-@tccutil reset PostEvent com.penpal.app 2>/dev/null && echo "  reset PostEvent" || true
	-@tccutil reset ScreenCapture com.penpal.app 2>/dev/null && echo "  reset ScreenCapture" || true

# onboard: launch interactive permission walkthrough using the installed .app
# binary, so prompt API runs under the correct bundle identity.
onboard:
	@echo "--- permission onboard ---"
	-$(APP_DEST)/Contents/MacOS/Penpal install onboard || true

# post-onboard-kick: route the final TCC refresh through the same runtime
# controller used by the HUD, watchdog, and CLI.
post-onboard-kick:
	@echo "--- post-onboard daemon kick (refresh TCC cache) ---"
	$(PENPAL) restart

reload:
	PENPAL_EXECUTABLE=$(PENPAL) $(PENPAL) install launchd --force
