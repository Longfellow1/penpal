#!/usr/bin/env bash
set -euo pipefail

if [[ "${CI:-}" != "true" || "${GITHUB_ACTIONS:-}" != "true" ]]; then
  echo "FAIL ci_guard: requires CI=true and GITHUB_ACTIONS=true; use a throwaway GitHub macOS runner" >&2
  exit 2
fi

MODE="${1:-upgrade}"
if [[ "$MODE" != "upgrade" && "$MODE" != "fresh" ]]; then
  echo "Usage: $0 [upgrade|fresh]" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
export HOMEBREW_NO_AUTO_UPDATE=1
# The old keg must disappear on upgrade like on a user's Mac; runner images may disable that.
unset HOMEBREW_NO_INSTALL_CLEANUP

DRILL_TMP="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/penpal-upgrade-drill.XXXXXX")"
trap 'rm -rf "$DRILL_TMP"' EXIT

# Every external call is bounded: a hung launchctl/penpal/brew must fail a check, not stall the job for 45 min.
# No `timeout` binary on the runner, so poll a background child (5 polls per second).
run_bounded() {
  local limit="$1" pid rc=0 ticks=0
  shift
  "$@" &
  pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    if (( ticks >= limit * 5 )); then
      kill -TERM "$pid" 2>/dev/null || true
      sleep 3
      kill -KILL "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
      printf 'TIMEOUT after %ss: %s\n' "$limit" "$*" >&2
      return 124
    fi
    sleep 0.2
    ticks=$((ticks + 1))
  done
  wait "$pid" || rc=$?
  return "$rc"
}

launchctl() { run_bounded 15 command launchctl "$@"; }
brew() {
  case "${1:-}" in
    install|upgrade|reinstall|cleanup|tap) run_bounded 900 command brew "$@" ;;
    *) run_bounded 60 command brew "$@" ;;
  esac
}
penpal() {
  case "${1:-}" in
    install|restart|start|stop) run_bounded 120 command penpal "$@" ;;
    *) run_bounded 60 command penpal "$@" ;;
  esac
}

CHECK_REASON=""
BREW_PREFIX=""
FORMULA_PATH=""
EXPECTED_BIN=""
GUI_DOMAIN="gui/$(id -u)"
DAEMON_TARGET="$GUI_DOMAIN/com.penpal.daemon"
HEALTH_TARGET="$GUI_DOMAIN/com.penpal.healthcheck"
LEGACY_TARGET="$GUI_DOMAIN/com.keypulse.daemon"
LEGACY_PLIST="$HOME/Library/LaunchAgents/com.keypulse.daemon.plist"
DRILL_VERSION_1=""
DRILL_VERSION_2=""
DRILL_VERSION_3=""
LAST_FINGERPRINT=""
CANDIDATE_FINGERPRINT_1=""
OLD_PID=""
OLD_DB_PATH=""
HEALTH_RUNS_BEFORE_UPGRADE=""
STATUS_OUTPUT=""
STATUS_RC=0
JOB_OUTPUT=""
JOB_RC=0

check() {
  local name="$1"
  shift
  CHECK_REASON=""
  if "$@"; then
    printf 'PASS %s\n' "$name"
  else
    printf 'FAIL %s: %s\n' "$name" "${CHECK_REASON:-check failed}" >&2
    dump_diagnostics
    exit 1
  fi
}

dump_diagnostics() {
  local target log plist
  set +e
  printf '\n=== diagnostics: launchd jobs ===\n'
  for target in "$DAEMON_TARGET" "$HEALTH_TARGET" "$LEGACY_TARGET"; do
    printf '\n--- launchctl print %s ---\n' "$target"
    launchctl print "$target" 2>&1
  done

  printf '\n=== diagnostics: last 50 lines of ~/.penpal/logs/* ===\n'
  for log in "$HOME"/.penpal/logs/*; do
    [[ -f "$log" ]] || continue
    printf '\n--- %s ---\n' "$log"
    tail -n 50 "$log"
  done

  printf '\n=== diagnostics: penpal doctor --json ===\n'
  penpal doctor --json 2>&1
  printf '\n=== diagnostics: brew list --versions penpal ===\n'
  brew list --versions penpal 2>&1

  printf '\n=== diagnostics: launch agent plists ===\n'
  for plist in "$HOME"/Library/LaunchAgents/com.penpal*.plist "$HOME"/Library/LaunchAgents/com.keypulse*.plist; do
    [[ -f "$plist" ]] || continue
    printf '\n--- %s ---\n' "$plist"
    plutil -p "$plist" 2>&1 || cat "$plist"
  done
  set -e
}

fail_check() {
  CHECK_REASON="$*"
  return 1
}

check_runner_tools() {
  local tool
  if [[ "$(uname)" != "Darwin" ]]; then
    fail_check "macOS is required"
    return 1
  fi
  for tool in brew launchctl git tar ruby shasum python3 plutil awk grep sed tail sqlite3; do
    if ! type -P "$tool" >/dev/null 2>&1; then
      fail_check "required command is unavailable: $tool"
      return 1
    fi
  done
  if ! BREW_PREFIX="$(brew --prefix 2>&1)"; then
    fail_check "brew --prefix failed: $BREW_PREFIX"
    return 1
  fi
  EXPECTED_BIN="$BREW_PREFIX/bin/penpal"
  if [[ ! -f "$REPO_DIR/tests/read_only_commands.txt" ]]; then
    fail_check "shared read-only command list is missing from the checkout"
    return 1
  fi
}

status_field() {
  local key="$1"
  printf '%s\n' "$STATUS_OUTPUT" | awk -F= -v wanted="$key" '$1 == wanted { sub(/^[^=]*=/, ""); print; exit }'
}

read_status() {
  STATUS_RC=0
  if STATUS_OUTPUT="$(penpal status --plain 2>&1)"; then
    STATUS_RC=0
  else
    STATUS_RC=$?
  fi
}

status_summary() {
  printf '%s\n' "$STATUS_OUTPUT" | awk -F= '$1 == "runtime_state" || $1 == "pid" || $1 == "db_path" || $1 == "db_size_mb" { printf "%s%s=%s", separator, $1, $2; separator=" " }'
}

wait_for_state() {
  local wanted="$1" timeout="$2" deadline state pid supervised
  deadline=$((SECONDS + timeout))
  while true; do
    read_status
    state="$(status_field runtime_state)"
    # The previous release predates runtime_state and reports the same value as `status`.
    [[ -n "$state" ]] || state="$(status_field status)"
    pid="$(status_field pid)"
    supervised="$(status_field supervised_by_launchd)"
    if [[ "$STATUS_RC" == 0 && "$state" == "$wanted" ]]; then
      if [[ "$wanted" != "running" || ( "$pid" =~ ^[0-9]+$ && ( "$supervised" == "True" || "$supervised" == "true" ) ) ]]; then
        return 0
      fi
    fi
    if (( SECONDS >= deadline )); then
      fail_check "runtime_state=$wanted not observed within ${timeout}s; latest (rc=$STATUS_RC): $(status_summary)"
      printf 'last status --plain output:\n%s\n' "$STATUS_OUTPUT" >&2
      return 1
    fi
    sleep 2
  done
}

job_print() {
  JOB_RC=0
  if JOB_OUTPUT="$(launchctl print "$1" 2>&1)"; then
    JOB_RC=0
  else
    JOB_RC=$?
  fi
}

job_runs() {
  printf '%s\n' "$JOB_OUTPUT" | awk '/^[[:space:]]*runs = [0-9]+/ { print $3; exit }'
}

job_last_exit() {
  printf '%s\n' "$JOB_OUTPUT" | sed -nE 's/.*last exit code = (-?[0-9]+).*/\1/p' | tail -n 1
}

job_is_running() {
  printf '%s\n' "$JOB_OUTPUT" | grep -Eq '^[[:space:]]*state = running[[:space:]]*$'
}

wait_for_healthcheck_success() {
  # want_exit "0" for a healthy daemon; "any" when the daemon is stopped on purpose (healthcheck exits 1 then).
  local baseline="$1" timeout="$2" want_exit="${3:-0}" deadline runs exit_code
  deadline=$((SECONDS + timeout))
  while true; do
    job_print "$HEALTH_TARGET"
    runs="$(job_runs)"
    exit_code="$(job_last_exit)"
    if [[ "$JOB_RC" == 0 && "$runs" =~ ^[0-9]+$ ]] && (( runs > baseline )) && [[ "$want_exit" == "any" || "$exit_code" == "$want_exit" ]] && ! job_is_running; then
      return 0
    fi
    if (( SECONDS >= deadline )); then
      fail_check "healthcheck has not completed successfully (baseline=$baseline current=$runs last_exit=$exit_code)"
      return 1
    fi
    sleep 1
  done
}

# Callers run the installed interpreter with -I: from the checkout, a plain `python -c` would import the
# checkout's penpal (cwd on sys.path) instead of the installed package and report the wrong identity.
penpal_python() {
  local entrypoint shebang resolved
  entrypoint="$(type -P penpal 2>/dev/null || true)"
  if [[ -z "$entrypoint" || ! -f "$entrypoint" ]]; then
    return 1
  fi
  IFS= read -r shebang < "$entrypoint" || true
  shebang="${shebang#\#!}"
  if [[ -x "$shebang" ]]; then
    printf '%s\n' "$shebang"
    return 0
  fi
  case "$shebang" in
    /usr/bin/env\ *)
      resolved="$(command -v "${shebang#/usr/bin/env }" 2>/dev/null || true)"
      if [[ -n "$resolved" && -x "$resolved" ]]; then
        printf '%s\n' "$resolved"
        return 0
      fi
      ;;
  esac
  for resolved in "$(brew --prefix penpal 2>/dev/null)/libexec/bin/python" "$(brew --prefix penpal 2>/dev/null)/libexec/bin/python3"; do
    if [[ -x "$resolved" ]]; then
      printf '%s\n' "$resolved"
      return 0
    fi
  done
  return 1
}

installed_fingerprint() {
  local python
  if ! python="$(penpal_python)"; then
    return 1
  fi
  "$python" -I -c 'from penpal.foundation.runtime_identity import installed_fingerprint; print(installed_fingerprint())'
}

assert_daemon_identity_matches_install() {
  local expected_fingerprint="$1" expected_package_version="$2" python result
  if ! python="$(penpal_python)"; then
    fail_check "could not resolve the installed Penpal Python interpreter"
    return 1
  fi
  if ! result="$("$python" -I - "$HOME/.penpal/run/daemon.json" <<'PY'
import json
import sys
from penpal.foundation.runtime_identity import installed_fingerprint, installed_package_version

record_path = sys.argv[1]
with open(record_path, encoding="utf-8") as handle:
    record = json.load(handle)
print(record.get("code_fingerprint", ""))
print(record.get("version", ""))
print(installed_fingerprint())
print(installed_package_version())
PY
)"; then
    fail_check "could not read installed and daemon runtime identities"
    return 1
  fi
  local recorded_fingerprint recorded_version installed_hash installed_version
  recorded_fingerprint="$(printf '%s\n' "$result" | sed -n '1p')"
  recorded_version="$(printf '%s\n' "$result" | sed -n '2p')"
  installed_hash="$(printf '%s\n' "$result" | sed -n '3p')"
  installed_version="$(printf '%s\n' "$result" | sed -n '4p')"
  if [[ "$installed_hash" != "$expected_fingerprint" || "$installed_version" != "$expected_package_version" ]]; then
    fail_check "installed identity changed while checking it (fingerprint=$installed_hash version=$installed_version)"
    return 1
  fi
  if [[ "$recorded_fingerprint" != "$installed_hash" || "$recorded_version" != "$installed_version" ]]; then
    fail_check "daemon record is not on the installed identity (daemon=$recorded_version/$recorded_fingerprint installed=$installed_version/$installed_hash)"
    return 1
  fi
}

daemon_record_fingerprint() {
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("code_fingerprint", ""))' \
    "$HOME/.penpal/run/daemon.json" 2>/dev/null || true
}

candidate_versions() {
  local base_version
  base_version="$(awk -F '"' '/^version = / { print $2; exit }' "$REPO_DIR/pyproject.toml")"
  if [[ -z "$base_version" ]]; then
    fail_check "could not read the project version from pyproject.toml"
    return 1
  fi
  DRILL_VERSION_1="${base_version}.99-drill.1"
  DRILL_VERSION_2="${base_version}.99-drill.2"
  DRILL_VERSION_3="${base_version}.99-drill.3"
}

ensure_tap() {
  if [[ ! -d "$(brew --repository)/Library/Taps/longfellow1/homebrew-penpal" ]]; then
    if ! brew tap longfellow1/penpal; then
      fail_check "could not tap longfellow1/penpal"
      return 1
    fi
  fi
  FORMULA_PATH="$(brew --repository)/Library/Taps/longfellow1/homebrew-penpal/Formula/penpal.rb"
  if [[ ! -f "$FORMULA_PATH" ]]; then
    fail_check "tap formula not found: $FORMULA_PATH"
    return 1
  fi
}

build_candidate_tarball() {
  local version="$1" add_identity_marker="${2:-false}" tarball marker_dir marker
  tarball="$DRILL_TMP/penpal-$version.tar"
  if ! git -C "$REPO_DIR" archive --format=tar HEAD > "$tarball"; then
    fail_check "git archive failed for candidate $version"
    return 1
  fi
  if [[ "$add_identity_marker" == "true" ]]; then
    marker_dir="$DRILL_TMP/identity-marker"
    marker="upgrade_drill_identity_generated.py"
    mkdir -p "$marker_dir/penpal"
    # The version in the body makes each drill candidate's fingerprint distinct.
    printf '"""Harmless generated module that changes the upgrade-drill package fingerprint (%s)."""\n' \
      "$version" > "$marker_dir/penpal/$marker"
    if ! tar -rf "$tarball" -C "$marker_dir" "penpal/$marker"; then
      fail_check "could not append the generated identity marker to candidate $version"
      return 1
    fi
  fi
  TAR_PATH="$tarball"
  TAR_SHA="$(shasum -a 256 "$tarball" | awk '{print $1}')"
}

rewrite_formula() {
  local version="$1" tar_path="$2" tar_sha="$3" file_url
  file_url="file://$tar_path"
  if ! ruby - "$FORMULA_PATH" "$file_url" "$tar_sha" "$version" <<'RUBY'
path, url, sha, version = ARGV
lines = File.readlines(path)
targets = {
  /^  url\s+/ => "  url #{url.dump}\n",
  /^  sha256\s+/ => "  sha256 #{sha.dump}\n",
  /^  version\s+/ => "  version #{version.dump}\n",
}
targets.each do |pattern, replacement|
  indexes = lines.each_index.select { |index| lines[index].match?(pattern) }
  abort "expected exactly one top-level #{pattern} in #{path}, found #{indexes.length}" unless indexes.length == 1
  lines[indexes.first] = replacement
end
File.write(path, lines.join)
RUBY
  then
    fail_check "could not rewrite the tapped formula for $version"
    return 1
  fi
}

install_from_checkout() {
  local log_path="$DRILL_TMP/install-$MODE.log"
  if ! (cd "$HOME" && run_bounded 900 bash "$REPO_DIR/install.sh") > "$log_path" 2>&1; then
    cat "$log_path"
    fail_check "candidate install.sh failed in public mode"
    return 1
  fi
  cat "$log_path"
  if ! grep -Fq 'Homebrew public install' "$log_path"; then
    fail_check "candidate install.sh did not report Homebrew public install mode"
    return 1
  fi
}

prepare_candidate_one() {
  if ! candidate_versions; then return 1; fi
  if ! ensure_tap; then return 1; fi
  if ! build_candidate_tarball "$DRILL_VERSION_1"; then return 1; fi
  if ! rewrite_formula "$DRILL_VERSION_1" "$TAR_PATH" "$TAR_SHA"; then return 1; fi
}

plant_legacy_job() {
  local python
  if launchctl print "$LEGACY_TARGET" >/dev/null 2>&1; then
    fail_check "legacy launchd label already exists on the runner: $LEGACY_TARGET"
    return 1
  fi
  mkdir -p "$(dirname "$LEGACY_PLIST")"
  cat > "$LEGACY_PLIST" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.keypulse.daemon</string>
  <key>ProgramArguments</key><array><string>/bin/sleep</string><string>600</string></array>
  <key>RunAtLoad</key><true/>
</dict>
</plist>
PLIST
  if ! launchctl bootstrap "$GUI_DOMAIN" "$LEGACY_PLIST"; then
    fail_check "launchctl bootstrap failed for the planted legacy job"
    return 1
  fi
  if ! launchctl kickstart "$LEGACY_TARGET"; then
    fail_check "launchctl kickstart failed for the planted legacy job"
    return 1
  fi
  if ! launchctl print "$LEGACY_TARGET" >/dev/null 2>&1; then
    fail_check "legacy job did not remain loaded after bootstrap and kickstart"
    return 1
  fi
  python="$(command -v python3)"
  "$python" -I - "$LEGACY_PLIST" <<'PY'
import plistlib
import sys
with open(sys.argv[1], "rb") as handle:
    value = plistlib.load(handle)
assert value["ProgramArguments"] == ["/bin/sleep", "600"]
PY
}

install_previous_release() {
  if ! brew install longfellow1/penpal/penpal; then
    fail_check "could not install the previous public release"
    return 1
  fi
  if ! penpal install init; then
    fail_check "previous public release penpal install init failed"
    return 1
  fi
  if ! wait_for_state running 60; then
    CHECK_REASON="previous public release did not reach running within 60s; baseline assumption broke ($(status_summary))"
    return 1
  fi
  OLD_PID="$(status_field pid)"
  OLD_DB_PATH="$(status_field db_path)"
  if [[ ! "$OLD_PID" =~ ^[0-9]+$ ]]; then
    fail_check "previous release did not report a numeric pid"
    return 1
  fi
  if [[ -z "$OLD_DB_PATH" ]]; then
    fail_check "previous release status did not report db_path"
    return 1
  fi
  job_print "$HEALTH_TARGET"
  if [[ "$JOB_RC" != 0 || ! "$(job_runs)" =~ ^[0-9]+$ ]]; then
    fail_check "previous release healthcheck job is not loaded or does not report a run count"
    return 1
  fi
  HEALTH_RUNS_BEFORE_UPGRADE="$(job_runs)"
  printf 'previous_release_pid=%s db_path=%s healthcheck_runs=%s\n' "$OLD_PID" "$OLD_DB_PATH" "$HEALTH_RUNS_BEFORE_UPGRADE"
}

upgrade_to_candidate_one() {
  job_print "$HEALTH_TARGET"
  if [[ "$JOB_RC" != 0 || ! "$(job_runs)" =~ ^[0-9]+$ ]]; then
    fail_check "could not record healthcheck run count before upgrade"
    return 1
  fi
  HEALTH_RUNS_BEFORE_UPGRADE="$(job_runs)"
  if ! prepare_candidate_one; then return 1; fi
  if ! install_from_checkout; then return 1; fi
  local installed_version
  installed_version="$(brew list --versions penpal | awk '{ print $2; exit }')"
  if [[ "$installed_version" != "$DRILL_VERSION_1" ]]; then
    fail_check "brew installed '$installed_version', expected '$DRILL_VERSION_1'"
    return 1
  fi
}

prepare_fresh_candidate() {
  HEALTH_RUNS_BEFORE_UPGRADE=0
  if ! prepare_candidate_one; then return 1; fi
}

check_candidate_version() {
  local installed_version
  installed_version="$(brew list --versions penpal | awk '{ print $2; exit }')"
  if [[ "$installed_version" != "$DRILL_VERSION_1" ]]; then
    fail_check "brew installed '$installed_version', expected '$DRILL_VERSION_1'"
    return 1
  fi
}

check_legacy_removed() {
  if launchctl print "$LEGACY_TARGET" >/dev/null 2>&1; then
    fail_check "legacy launchd job remains loaded after install.sh"
    return 1
  fi
  if [[ -e "$LEGACY_PLIST" ]]; then
    fail_check "legacy launchd plist remains on disk: $LEGACY_PLIST"
    return 1
  fi
}

check_stable_plist_entries() {
  local label plist python
  if ! python="$(penpal_python)"; then
    fail_check "could not resolve installed Python for plist validation"
    return 1
  fi
  for label in com.penpal.daemon com.penpal.healthcheck; do
    plist="$HOME/Library/LaunchAgents/$label.plist"
    if [[ ! -f "$plist" ]]; then
      fail_check "required launchd plist is missing: $plist"
      return 1
    fi
    if ! "$python" -I - "$plist" "$EXPECTED_BIN" <<'PY'
import plistlib
import sys
with open(sys.argv[1], "rb") as handle:
    value = plistlib.load(handle)
assert value.get("ProgramArguments", [None])[0] == sys.argv[2]
PY
    then
      fail_check "$label ProgramArguments[0] does not equal $EXPECTED_BIN"
      return 1
    fi
  done
}

check_candidate_running() {
  local python package_version
  if ! wait_for_state running 90; then return 1; fi
  if [[ "$MODE" == "upgrade" && "$(status_field pid)" == "$OLD_PID" ]]; then
    fail_check "candidate daemon reused old pid $OLD_PID instead of restarting"
    return 1
  fi
  CANDIDATE_FINGERPRINT_1="$(installed_fingerprint)" || {
    fail_check "could not compute installed candidate fingerprint"
    return 1
  }
  if [[ -z "$CANDIDATE_FINGERPRINT_1" ]]; then
    fail_check "installed candidate fingerprint is empty"
    return 1
  fi
  if ! python="$(penpal_python)"; then
    fail_check "could not resolve installed Python to verify the daemon identity"
    return 1
  fi
  package_version="$("$python" -I -c 'from penpal.foundation.runtime_identity import installed_package_version; print(installed_package_version())')"
  if ! assert_daemon_identity_matches_install "$CANDIDATE_FINGERPRINT_1" "$package_version"; then
    return 1
  fi
}

check_healthcheck_after_upgrade() {
  local runs exit_code
  job_print "$HEALTH_TARGET"
  if [[ "$JOB_RC" != 0 ]]; then
    fail_check "healthcheck job is not loaded after install.sh"
    return 1
  fi
  runs="$(job_runs)"
  if [[ ! "$runs" =~ ^[0-9]+$ ]]; then
    fail_check "launchctl did not report healthcheck runs"
    return 1
  fi
  exit_code="$(job_last_exit)"
  if (( runs <= HEALTH_RUNS_BEFORE_UPGRADE )) || { [[ "$exit_code" != "0" ]] && ! job_is_running; }; then
    if ! launchctl kickstart "$HEALTH_TARGET"; then
      fail_check "healthcheck had not completed successfully since upgrade and kickstart failed"
      return 1
    fi
  fi
  if ! wait_for_healthcheck_success "$HEALTH_RUNS_BEFORE_UPGRADE" 20; then return 1; fi
}

check_read_only_commands() {
  local line index=0 rc output_path
  local -a command_args
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" || "$line" == \#* ]] && continue
    read -r -a command_args <<< "$line"
    output_path="$DRILL_TMP/read-only-$index.out"
    rc=0
    printf 'read-only command: penpal %s\n' "$line"
    penpal "${command_args[@]}" > "$output_path" 2>&1 || rc=$?
    cat "$output_path"
    if [[ "$rc" != 0 && "$rc" != 1 ]]; then
      fail_check "penpal $line exited $rc (expected 0 or 1)"
      return 1
    fi
    if grep -q 'Traceback' "$output_path"; then
      fail_check "penpal $line emitted a Python traceback"
      return 1
    fi
    if [[ "$line" == "doctor --json" ]]; then
      local python
      if ! python="$(penpal_python)" || ! "$python" -I -c 'import json,sys; value=json.load(sys.stdin); assert isinstance(value.get("checks"), dict)' < "$output_path"; then
        fail_check "penpal doctor --json did not emit the expected machine-readable checks object"
        return 1
      fi
    fi
    index=$((index + 1))
  done < "$REPO_DIR/tests/read_only_commands.txt"
}

check_database_state() {
  local db_path size_mb
  read_status
  if [[ "$STATUS_RC" != 0 ]]; then
    fail_check "penpal status --plain exited $STATUS_RC after upgrade: $(status_summary)"
    return 1
  fi
  db_path="$(status_field db_path)"
  size_mb="$(status_field db_size_mb)"
  if [[ -z "$db_path" ]]; then
    fail_check "status did not report db_path"
    return 1
  fi
  if [[ "$MODE" == "upgrade" && "$db_path" != "$OLD_DB_PATH" ]]; then
    fail_check "status db_path changed across upgrade (before=$OLD_DB_PATH after=$db_path)"
    return 1
  fi
  # db_size_mb counts only the main file; a fresh WAL-mode DB keeps its pages in -wal, so size can read 0.
  local integrity
  integrity="$(sqlite3 -readonly "$db_path" 'PRAGMA quick_check;' 2>&1 || true)"
  if [[ "$integrity" != "ok" ]]; then
    fail_check "database does not open cleanly (db_path=$db_path db_size_mb=$size_mb quick_check=$integrity)"
    return 1
  fi
  if [[ "$(sqlite3 -readonly "$db_path" "SELECT count(*) FROM sqlite_master WHERE type='table';" 2>/dev/null)" -lt 1 ]]; then
    fail_check "database has no tables (db_path=$db_path)"
    return 1
  fi
}

check_restart_cycle() {
  local before_pid new_pid
  read_status
  before_pid="$(status_field pid)"
  if ! penpal restart; then
    fail_check "penpal restart failed"
    return 1
  fi
  if ! wait_for_state running 60; then return 1; fi
  new_pid="$(status_field pid)"
  if [[ ! "$new_pid" =~ ^[0-9]+$ || "$new_pid" == "$before_pid" ]]; then
    fail_check "restart did not produce a new running daemon pid (before=$before_pid after=$new_pid)"
    return 1
  fi
}

check_stop_watchdog_start() {
  local stopped_pid deadline state runs_before
  read_status
  stopped_pid="$(status_field pid)"
  if ! penpal stop; then
    fail_check "penpal stop failed"
    return 1
  fi
  if ! wait_for_state not_running 30; then return 1; fi
  job_print "$HEALTH_TARGET"
  runs_before="$(job_runs)"
  if [[ "$JOB_RC" != 0 || ! "$runs_before" =~ ^[0-9]+$ ]]; then
    fail_check "healthcheck job is not loaded before the stop/watchdog check"
    return 1
  fi
  if ! launchctl kickstart "$HEALTH_TARGET"; then
    fail_check "could not kickstart the healthcheck watchdog after stop"
    return 1
  fi
  if ! wait_for_healthcheck_success "$runs_before" 20 any; then return 1; fi
  local started_pid
  deadline=$((SECONDS + 30))
  while true; do
    read_status
    state="$(status_field runtime_state)"
    if [[ "$STATUS_RC" != 0 || "$state" != "not_running" ]]; then
      fail_check "watchdog revived or obscured a user stop (runtime_state=${state:-unknown})"
      return 1
    fi
    if (( SECONDS >= deadline )); then break; fi
    sleep 2
  done
  if ! penpal start; then
    fail_check "penpal start failed after the stop/watchdog check"
    return 1
  fi
  if ! wait_for_state running 60; then return 1; fi
  started_pid="$(status_field pid)"
  if [[ ! "$started_pid" =~ ^[0-9]+$ || "$started_pid" == "$stopped_pid" ]]; then
    fail_check "penpal start did not create a new daemon pid (stopped=$stopped_pid started=$started_pid)"
    return 1
  fi
}

# Upgrade with `brew upgrade` only (no install init, no restart) and wait for the daemon to exec the new code in
# place. keep_old_keg=true mimics HOMEBREW_NO_INSTALL_CLEANUP: the keg the daemon was loaded from stays on disk,
# so the daemon must follow the stable entry rather than re-hash its own keg.
self_reload_to() {
  local from_version="$1" to_version="$2" keep_old_keg="$3"
  local pid_before old_keg python package_version fingerprint_after
  read_status
  pid_before="$(status_field pid)"
  if [[ ! "$pid_before" =~ ^[0-9]+$ ]]; then
    fail_check "could not capture daemon pid before self-reload"
    return 1
  fi
  old_keg="$(brew --cellar)/penpal/$from_version"
  if ! build_candidate_tarball "$to_version" true; then return 1; fi
  if ! rewrite_formula "$to_version" "$TAR_PATH" "$TAR_SHA"; then return 1; fi
  if [[ "$keep_old_keg" == "true" ]]; then
    if ! (export HOMEBREW_NO_INSTALL_CLEANUP=1; brew upgrade longfellow1/penpal/penpal); then
      fail_check "brew upgrade to $to_version failed"
      return 1
    fi
    if [[ ! -d "$old_keg" ]]; then
      fail_check "scenario setup: $from_version keg was removed although cleanup was disabled"
      return 1
    fi
  else
    if ! brew upgrade longfellow1/penpal/penpal; then
      fail_check "brew upgrade to $to_version failed"
      return 1
    fi
    brew cleanup penpal || true
  fi
  local deadline state pid installed_version
  deadline=$((SECONDS + 180))
  while true; do
    read_status
    state="$(status_field runtime_state)"
    pid="$(status_field pid)"
    if [[ "$STATUS_RC" == 0 && "$state" == "running" && "$pid" == "$pid_before" ]]; then
      if ! fingerprint_after="$(installed_fingerprint)"; then
        fail_check "could not compute the $to_version fingerprint"
        return 1
      fi
      # Files on disk change at once; the reload is done only when the daemon's own record carries them.
      if [[ "$fingerprint_after" != "$LAST_FINGERPRINT" && "$(daemon_record_fingerprint)" == "$fingerprint_after" ]]; then
        break
      fi
    fi
    if (( SECONDS >= deadline )); then
      fail_check "daemon did not self-reload to $to_version with the same pid within 180s (state=${state:-unknown} pid=${pid:-none} keep_old_keg=$keep_old_keg)"
      return 1
    fi
    sleep 2
  done
  # With several kegs on disk, the linked one is the installed version.
  installed_version="$(basename "$(readlink "$(brew --prefix)/opt/penpal")")"
  if [[ "$installed_version" != "$to_version" ]]; then
    fail_check "brew reports '$installed_version' after the upgrade; expected '$to_version'"
    return 1
  fi
  if [[ "$keep_old_keg" != "true" && -e "$old_keg" ]]; then
    fail_check "$from_version keg still exists after cleanup: $old_keg"
    return 1
  fi
  if ! python="$(penpal_python)"; then
    fail_check "could not resolve installed Python after self-reload"
    return 1
  fi
  package_version="$("$python" -I -c 'from penpal.foundation.runtime_identity import installed_package_version; print(installed_package_version())')"
  if ! assert_daemon_identity_matches_install "$fingerprint_after" "$package_version"; then return 1; fi
  LAST_FINGERPRINT="$fingerprint_after"
}

check_self_reload_keeping_old_keg() {
  LAST_FINGERPRINT="$CANDIDATE_FINGERPRINT_1"
  self_reload_to "$DRILL_VERSION_1" "$DRILL_VERSION_2" true
}

check_self_reload_after_cleanup() {
  # Both kegs are on disk now; cleanup removes every keg but the newest, including the one the daemon runs from.
  self_reload_to "$DRILL_VERSION_2" "$DRILL_VERSION_3" false
}

printf 'PASS ci_guard\n'
check runner_tools check_runner_tools

if [[ "$MODE" == "upgrade" ]]; then
  check previous_release_running install_previous_release
  # Planted after the previous release, whose own init removes legacy jobs: this tests the candidate.
  check legacy_job_planted plant_legacy_job
  check candidate_upgrade_install_sh upgrade_to_candidate_one
else
  check candidate_formula_prepared prepare_fresh_candidate
  check fresh_install_sh_public_mode install_from_checkout
  check candidate_version check_candidate_version
fi

if [[ "$MODE" == "upgrade" ]]; then
  check legacy_job_removed check_legacy_removed
fi
check stable_plist_entries check_stable_plist_entries
check candidate_daemon_running_and_identity check_candidate_running
check healthcheck_ran_successfully_after_install check_healthcheck_after_upgrade
check database_opens_and_reports_size check_database_state
check readonly_commands_do_not_traceback check_read_only_commands
check restart_reaches_running_with_new_pid check_restart_cycle
check stop_is_not_revived_by_watchdog_and_start_works check_stop_watchdog_start
if [[ "$MODE" == "upgrade" ]]; then
  check self_reload_with_old_keg_kept check_self_reload_keeping_old_keg
  check self_reload_after_brew_cleanup check_self_reload_after_cleanup
fi

printf 'Upgrade drill completed: mode=%s\n' "$MODE"
