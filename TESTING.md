# Penpal — Testing Guide

> Platform: macOS 12.0+  
> Runtime: Python 3.11+

---

## 1. Environment Setup

```bash
git clone https://github.com/longfellow1/penpal
cd penpal
pip install -e .
```

Verify installation:

```bash
penpal --help
```

The `upgrade-drill` GitHub Actions workflow tests fresh installation and upgrading the previous public release on macOS, including daemon start/stop/restart, watchdog behavior, and in-place self-reload.

---

## 2. Pre-flight Check

Run the doctor command to verify dependencies and permissions:

```bash
penpal doctor
```

Expected output:

```
Python >= 3.11          OK
pyobjc-framework-AppKit OK
pyobjc-framework-Quartz OK
Accessibility permission OK   ← must be OK for window watcher
DB path writable        OK
```

**If Accessibility shows FAIL:**  
System Settings → Privacy & Security → Accessibility → add your terminal app.

---

## 3. Daemon Lifecycle

### Start

```bash
penpal start
# Expected: daemon starts through its installed LaunchAgent
```

### Status

```bash
penpal status
# Expected: running=yes, enabled_watchers=window,idle,clipboard,manual
```

### Pause / Resume

```bash
penpal pause
penpal status   # status=paused
penpal resume
penpal status   # status=running
```

### Stop

```bash
penpal stop
# Expected: daemon LaunchAgent is unloaded
penpal status   # running=no
```

### R7: menu-bar HUD identity and visibility (owner manual check)

Run this once on the owner’s Mac after installing the app. Do not launch the bundle executable from Terminal:

1. Run `penpal app install`.
2. In Finder, open `~/Applications/Penpal.app` (or the custom `--dir` location).
3. Open System Settings → Menu Bar. On macOS 26, confirm **Allow in the Menu Bar → Penpal** is enabled; the menu-bar icon should be visible. On macOS 15, confirm the icon is visible in the menu bar.
4. Confirm the app is listed as **Penpal**, not **Python**, in the menu-bar app list where the OS provides that list.
5. Run `penpal doctor` and `penpal hud status`; the HUD row should report a recent main-thread tick and a visible status item.
6. If `penpal app install` reports the clang/libpython fallback, the entry may be named **Python**. Install the app from the Penpal environment that has clang and libpython, then repeat from Finder.

To remove old entries, quit any old HUD with `penpal hud stop`, then move the old `/Applications/KeyPulse.app` to Trash in Finder. On macOS 26, if an obsolete **Python** entry remains under System Settings → Menu Bar → Allow in the Menu Bar, turn it off only after confirming it belongs to the old Penpal launcher. Do not remove a Python entry used by another app.

Visibility is best-effort in `hud.json`: `NSStatusItem.isVisible()` plus the status button window’s `occlusionState` when available. macOS 26 does not expose the System Settings allow/deny switch through a public API, and `isVisible()` may remain true when that switch is off. The manual check is therefore the R7 authority.

### PR 5: legacy files and log cleanup (owner manual check)

1. Run `penpal doctor` and confirm its **遗留文件 / Legacy files** section lists the old KeyPulse.app if one is present.
2. Run `penpal maintenance clean-legacy`; it should remove only old PID/log files inside `~/.penpal`.
3. Run `penpal doctor` again. Confirm data-dir files are gone, while an old app remains listed until you move it to Trash yourself.

### R4: in-place upgrade with three MCP hosts

Run this checklist once each in Claude Code, Codex, and Cursor on the owner’s Mac. Keep the same host session and conversation open during the upgrade. Use a distinctive keyword from an existing, authorized Penpal record so the query has a known result.

1. Open a session in the host and confirm Penpal is connected.
2. Ask: `Use Penpal to search my recent history for <known distinctive keyword>.` Confirm that the expected record is returned.
3. In a separate terminal, upgrade the installed package with `brew upgrade penpal`, or run `make reload` from this repository for a development install. Wait for the command to finish.
4. In the original host session, ask the same query again without reconnecting or opening a new conversation. It should return the expected record; the host should not report a broken stdio connection or require a new initialize handshake.
5. Repeat steps 1–4 in the other two hosts. After all three hosts are fully closed, run `pgrep -fl "penpal mcp serve"`. It should print no processes. While a host is open, only its active Penpal server should appear; closing that host should remove its process.

---

## 4. Data Capture — Manual Verification

Start the daemon, switch between a few apps, then:

```bash
penpal timeline --today
```

Expected: table of sessions showing app names, window titles, duration.

### Clipboard capture

Copy some text in any app, wait 2 seconds, then:

```bash
penpal recent --type clipboard --limit 5
```

Expected: your copied text appears (possibly desensitised if it matched privacy patterns).

### Manual save

```bash
penpal save --text "Test note for penpal" --tag test
penpal recent --type manual
```

Expected: your note appears.

Via stdin:

```bash
echo "stdin note" | penpal save --tag test
```

---

## 5. Search

```bash
penpal search "penpal"
penpal search "penpal" --app Terminal
penpal search "penpal" --since 1d
penpal search "penpal" --source manual
```

Expected: results table with Time / Type / App / Content columns.

Plain output (for scripting / skill use):

```bash
penpal search "penpal" --plain
# TSV: timestamp\ttype\tapp\ttitle\tbody
```

---

## 6. Stats & Export

```bash
penpal stats --days 7
penpal stats --days 7 --plain
```

```bash
penpal export --format json --days 1
penpal export --format csv  --days 1
penpal export --format md   --date $(date +%Y-%m-%d)
penpal export --format json --output /tmp/kp_export.json --days 7
```

---

## 7. Session Commands

```bash
penpal session list
penpal session list --date $(date +%Y-%m-%d)
```

Grab an ID from the output, then:

```bash
penpal session show <session-id>
```

---

## 8. Privacy & Policy

### Verify desensitisation

Copy a string containing a fake email or token, e.g.:  
`token=sk-abc123XYZ user@example.com`

Then check:

```bash
penpal recent --type clipboard --limit 1
```

Expected: email and token replaced with `[EMAIL]` / `[API_KEY]` markers.

### Policy rules

```bash
penpal rules list
penpal rules add --scope-type app --scope-value "1Password" --mode deny
penpal rules list   # new rule appears
penpal rules disable <id>
```

---

## 9. Purge & Retention

```bash
# Dry-run (shows what would be deleted)
penpal purge --last-hours 1

# Confirm deletion
penpal purge --last-hours 1 --confirm

# Delete by app
penpal purge --app Slack --confirm

# Clear everything from today
penpal purge --today --confirm
```

---

## 10. Config

```bash
penpal config show
penpal config path
```

Edit `~/.penpal/config.toml` to toggle watchers or adjust thresholds, then restart:

```bash
penpal restart
```

---

## 12. Single Instance Lock

Use a temporary HOME and run the foreground service directly:

```bash
PENPAL_TEST_HOME="$(mktemp -d)"
mkdir -p "$PENPAL_TEST_HOME/.penpal"
cat > "$PENPAL_TEST_HOME/.penpal/config.toml" <<'EOF'
[app]
flush_interval_sec = 1

[watchers]
window = false
idle = false
clipboard = false
manual = false
keyboard_chunk = false
browser = false
browser_url = false
ax_text = false
ocr = false

[sources.scheduler]
enabled = false
EOF
PENPAL_ISOLATED=1 HOME="$PENPAL_TEST_HOME" penpal serve
```

In a second terminal, use the same `PENPAL_TEST_HOME` value and run `penpal serve` again. The second process exits with code `73` and an already-running message. Stop the first process with `Ctrl-C`; the kernel releases its flock when the process exits.

PID files are diagnostics; changing one does not affect the lock.

---

## 13. Resource Usage

After 30 minutes of normal use:

```bash
ps aux | grep penpal
# CPU should be < 1%, RSS < 60 MB
```

```bash
ls -lh ~/.penpal/penpal.db
# Size should be reasonable (< 5 MB for a day of use)
```

## 14. Automated Tests and Opt-in Checks

The default pytest run uses synthetic fixtures and injected system probes. Performance benchmarks and
tests that call real macOS system commands are skipped unless explicitly enabled:

```bash
# Large synthetic migration and search timing checks
PENPAL_RUN_SLOW=1 python -m pytest -q -m slow

# Real Spotlight (mdimport / mdfind) and ioreg smoke checks on macOS
PENPAL_RUN_SYSTEM_SMOKE=1 python -m pytest -q -m system_smoke
```

The system smoke checks use temporary Spotlight files. The ioreg smoke check reads the current
machine's idle-time value and should only be run as an intentional local check.

---

## Known Limitations (MVP)

| Limitation | Notes |
|-----------|-------|
| macOS only | Linux/Windows watchers not implemented |
| Clipboard: text only | Images/files not indexed |
| Accessibility required | Without it, window titles fall back to app name only |
