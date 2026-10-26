#!/usr/bin/env bash
# Penpal — One-click installer for macOS
# Usage:
#   bash install.sh                 # public Homebrew install
#   bash install.sh --dev           # local editable install fallback
#   bash install.sh --no-brew       # alias for --dev
#   bash install.sh --dev --no-launchd
set -euo pipefail

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}  →${RESET} $*"; }
success() { echo -e "${GREEN}  ✓${RESET} $*"; }
warn()    { echo -e "${YELLOW}  ⚠${RESET} $*"; }
error()   { echo -e "${RED}  ✗${RESET} $*" >&2; }
header()  { echo -e "\n${BOLD}$*${RESET}"; }

# ── Flags ─────────────────────────────────────────────────────────────────────
INSTALL_LAUNCHD=true
DEV_MODE=false
NO_BREW=false
for arg in "$@"; do
  case $arg in
    --no-launchd) INSTALL_LAUNCHD=false ;;
    --dev)        DEV_MODE=true ;;
    --no-brew)    DEV_MODE=true; NO_BREW=true ;;
    -h|--help)
      cat <<'HELP'
Penpal installer

Usage:
  bash install.sh                 Install via Homebrew, then run penpal install init
  bash install.sh --dev           Developer fallback: editable install into ~/.penpal/venv
  bash install.sh --no-brew       Alias for --dev
  bash install.sh --dev --no-launchd
HELP
      exit 0
      ;;
    *) error "Unknown option: $arg"; exit 1 ;;
  esac
done

# ── Paths ─────────────────────────────────────────────────────────────────────
PENPAL_DIR="$HOME/.penpal"
VENV_DIR="$PENPAL_DIR/venv"
BIN_DIR="$HOME/.local/bin"
# Piped runs (README: curl ... | bash) have no BASH_SOURCE and no checkout. The Homebrew
# path must work there, so it fetches its one helper from the repo instead.
PENPAL_RAW_BASE="https://raw.githubusercontent.com/Longfellow1/penpal/main"
REPO_DIR=""
if [[ -n "${BASH_SOURCE[0]:-}" ]]; then
  REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
if [[ -n "$REPO_DIR" && -f "$REPO_DIR/scripts/compat-wrapper.sh" ]]; then
  source "$REPO_DIR/scripts/compat-wrapper.sh"
else
  COMPAT_TMP="$(mktemp)"
  curl -fsSL "$PENPAL_RAW_BASE/scripts/compat-wrapper.sh" -o "$COMPAT_TMP"
  source "$COMPAT_TMP"
  rm -f "$COMPAT_TMP"
fi
if [[ "$DEV_MODE" == true && -z "$REPO_DIR" ]]; then
  error "--dev needs a checkout. Run: git clone https://github.com/Longfellow1/penpal.git && cd penpal && bash install.sh --dev"
  exit 1
fi
CONFIG_FILE="$PENPAL_DIR/config.toml"

# ── Detect shell profile ───────────────────────────────────────────────────────
detect_profile() {
  if [[ -n "${ZSH_VERSION:-}" ]] || [[ "$SHELL" == */zsh ]]; then
    echo "$HOME/.zshrc"
  elif [[ -n "${BASH_VERSION:-}" ]] || [[ "$SHELL" == */bash ]]; then
    [[ -f "$HOME/.bash_profile" ]] && echo "$HOME/.bash_profile" || echo "$HOME/.bashrc"
  else
    echo "$HOME/.profile"
  fi
}

# ─────────────────────────────────────────────────────────────────────────────
header "Penpal Installer"
echo "  Repo:    ${REPO_DIR:-(none: piped run, Homebrew only)}"
if [[ "$DEV_MODE" == true ]]; then
  echo "  Mode:    developer editable install"
  echo "  Venv:    $VENV_DIR"
  echo "  Bin:     $BIN_DIR/penpal"
else
  echo "  Mode:    Homebrew public install"
  echo "  Tap:     Longfellow1/penpal"
  echo "  Formula: penpal"
fi
echo "  Config:  $CONFIG_FILE"
[[ "$DEV_MODE" == true ]] && warn "Dev mode enabled (editable install)"
[[ "$NO_BREW" == true ]] && warn "--no-brew selected; using developer fallback"

# ── 1. macOS check ────────────────────────────────────────────────────────────
header "1/7  Checking system"

if [[ "$(uname)" != "Darwin" ]]; then
  error "macOS required. Got: $(uname)"
  exit 1
fi
success "macOS $(sw_vers -productVersion)"

# Select Python before unloading old jobs; migration itself must happen only after
# the old KeyPulse launchd services have been booted out.
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" &>/dev/null; then
    ver=$("$candidate" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    major=${ver%%.*}; minor=${ver##*.}
    if [[ $major -ge 3 && $minor -ge 11 ]]; then
      PYTHON="$candidate"
      break
    fi
  fi
done

if [[ -z "$PYTHON" ]]; then
  error "Python 3.11+ not found."
  echo "  Install via Homebrew:  brew install python@3.12"
  echo "  Or via python.org:     https://www.python.org/downloads/"
  exit 1
fi
success "Python $($PYTHON --version 2>&1 | awk '{print $2}')  ($PYTHON)"

# Runs the legacy-job preflight and data-dir migration through Penpal's runtime modules.
check_data_dir_migration() {
  local py="$1"
  if ! "$py" -c '
import sys
from penpal.runtime.control import unload_legacy_jobs
failures = unload_legacy_jobs()
if failures:
    print("Could not unload legacy launchd jobs: " + "; ".join(failures), file=sys.stderr)
    sys.exit(1)
from penpal.utils.paths import DataDirMigrationError, get_data_dir
try:
    get_data_dir()
except DataDirMigrationError as exc:
    print(exc, file=sys.stderr)
    sys.exit(1)
'; then
    error "Data directory migration failed; the installer stopped without merging or copying data."
    exit 1
  fi
}

if [[ "$DEV_MODE" != true ]]; then
  header "2/4  Checking Homebrew"
  if ! command -v brew &>/dev/null; then
    error "Homebrew is required for the public installer."
    echo "  Install Homebrew first:"
    echo '  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
    exit 1
  fi
  success "Homebrew $(brew --version | head -n 1 | awk '{print $2}')"

  header "3/4  Installing Penpal"
  # Pre-rename formula: remove it before touching taps so the qualified install below is
  # unambiguous. Only the package is removed; user data in ~/.penpal is kept.
  if brew list --formula keypulse &>/dev/null; then
    info "Removing old keypulse formula (your data in ~/.penpal is not touched)…"
    brew uninstall keypulse || warn "Could not uninstall keypulse; run manually: brew uninstall keypulse"
  fi
  # Users of the pre-rename tap (Longfellow1/keypulse) get it removed. brew refuses to untap
  # while formulae from it are installed or it is untrusted; that is reported and tolerated.
  old_taps="$(brew tap 2>/dev/null || true)"
  if printf '%s\n' "$old_taps" | grep -qix 'longfellow1/keypulse'; then
    info "Removing old tap Longfellow1/keypulse…"
    if ! brew untap longfellow1/keypulse 2>/dev/null; then
      warn "Could not remove old tap Longfellow1/keypulse; if the install below fails, run: brew untap longfellow1/keypulse"
    fi
  fi
  # The fully qualified name auto-taps Longfellow1/penpal, so no separate tap step is needed.
  if brew list --formula penpal &>/dev/null; then
    info "Penpal already installed; upgrading…"
    brew upgrade longfellow1/penpal/penpal || brew reinstall longfellow1/penpal/penpal
  else
    brew install longfellow1/penpal/penpal
  fi
  success "penpal executable installed"
  # Use the formula's own Python: a piped run has no checkout to import penpal from.
  check_data_dir_migration "$(brew --prefix penpal)/libexec/bin/python"

  header "4/4  Initializing runtime"
  penpal install init
  mkdir -p "$BIN_DIR"
  penpal_install_compat_wrapper "$BIN_DIR/keypulse"

  echo ""
  echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
  echo -e "${GREEN}${BOLD}  Penpal installed successfully!${RESET}"
  echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
  echo ""
  echo "  Next steps:"
  echo -e "     ${CYAN}penpal install onboard${RESET}   # 选择要导入的历史来源（之前不读取历史）"
  echo -e "     ${CYAN}penpal doctor${RESET}"
  echo ""
  exit 0
fi

# ── 3. Create venv ────────────────────────────────────────────────────────────
export PYTHONPATH="$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}"
check_data_dir_migration "$PYTHON"

header "2/7  Setting up virtual environment"

mkdir -p "$PENPAL_DIR"

if [[ -d "$VENV_DIR" ]]; then
  stale_shebang=false
  for entrypoint in "$VENV_DIR/bin/penpal" "$VENV_DIR/bin/keypulse"; do
    [[ -f "$entrypoint" ]] || continue
    IFS= read -r shebang < "$entrypoint" || true
    if [[ "$shebang" == *"$HOME/.keypulse/venv/"* ]]; then
      stale_shebang=true
      break
    fi
  done
  if [[ "$stale_shebang" == true ]]; then
    warn "Moved venv entry point still targets ~/.keypulse/venv; rebuilding it under ~/.penpal/venv."
    rm -rf "$VENV_DIR"
    "$PYTHON" -m venv "$VENV_DIR"
    success "Venv rebuilt after data-directory migration"
  else
    warn "Venv already exists at $VENV_DIR — reusing"
  # Ensure it's usable
    if [[ ! -x "$VENV_DIR/bin/python" ]]; then
      warn "Venv looks broken, recreating…"
      rm -rf "$VENV_DIR"
      "$PYTHON" -m venv "$VENV_DIR"
      success "Venv recreated"
    fi
  fi
else
  info "Creating venv…"
  "$PYTHON" -m venv "$VENV_DIR"
  success "Venv created"
fi

VENV_PYTHON="$VENV_DIR/bin/python"
VENV_PIP="$VENV_DIR/bin/pip"

# Disable global pip --user config inside venv (Homebrew Python sets this globally)
export PIP_USER=false

# Upgrade pip silently (suppress version notice)
"$VENV_PIP" install --upgrade pip --quiet 2>/dev/null || true

# ── 4. Install Penpal ───────────────────────────────────────────────────────
header "3/7  Installing Penpal"

info "Editable install from $REPO_DIR"
"$VENV_PIP" install --no-user -e "$REPO_DIR" --quiet
success "Penpal installed"

# macOS dependencies come from the `macos` extra in pyproject.toml (the one list of pyobjc frameworks).
info "Installing macOS dependencies (pyobjc)…"
if "$VENV_PIP" install --no-user -e "$REPO_DIR[macos]" --quiet; then
  success "pyobjc installed"
else
  warn "pyobjc install failed — window/clipboard watchers will be disabled"
  warn "Install manually later:  $VENV_PIP install -e '$REPO_DIR[macos]'"
fi

# Quick sanity check
"$VENV_DIR/bin/penpal" --help &>/dev/null && \
  success "Binary works" || \
  { error "penpal binary not working after install"; exit 1; }

# ── 5. Wrapper + PATH ─────────────────────────────────────────────────────────
header "4/7  Installing penpal command"

mkdir -p "$BIN_DIR"

# Write wrapper that directly calls into venv (no activation needed)
cat > "$BIN_DIR/penpal" << WRAPPER
#!/usr/bin/env bash
exec "$VENV_DIR/bin/penpal" "\$@"
WRAPPER
chmod +x "$BIN_DIR/penpal"
success "Wrapper written: $BIN_DIR/penpal"
penpal_install_compat_wrapper "$BIN_DIR/keypulse"

# Auto-add ~/.local/bin to shell profile if not already there
PROFILE="$(detect_profile)"
PATH_LINE="export PATH=\"\$HOME/.local/bin:\$PATH\""

if echo "$PATH" | tr ':' '\n' | grep -qx "$BIN_DIR"; then
  success "~/.local/bin already in PATH"
else
  if grep -qF '.local/bin' "$PROFILE" 2>/dev/null; then
    warn "~/.local/bin line already in $PROFILE (but not active yet)"
  else
    echo "" >> "$PROFILE"
    echo "# Added by Penpal installer" >> "$PROFILE"
    echo "$PATH_LINE" >> "$PROFILE"
    success "Added ~/.local/bin to $PROFILE"
  fi
  warn "PATH updated — run: source $PROFILE"
  # Also export for the remainder of this script
  export PATH="$BIN_DIR:$PATH"
fi

# ── 6. Default config ─────────────────────────────────────────────────────────
header "5/7  Configuration"

if [[ -f "$CONFIG_FILE" ]]; then
  warn "Config already exists — skipping ($CONFIG_FILE)"
else
  cp "$REPO_DIR/config.toml" "$CONFIG_FILE"
  success "Default config written to $CONFIG_FILE"
fi

# ── 6. Runtime initialization and managed service restart ─────────────────────
header "6/7  Initializing runtime"
if [[ "$INSTALL_LAUNCHD" == false ]]; then
  "$VENV_DIR/bin/penpal" install init --no-launchd
else
  "$VENV_DIR/bin/penpal" install init
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo -e "${GREEN}${BOLD}  Penpal installed successfully!${RESET}"
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo ""
echo "  Next steps:"
echo ""
echo -e "  ${BOLD}1. Reload your shell (PATH update):${RESET}"
echo -e "     ${CYAN}source $PROFILE${RESET}"
echo ""
echo -e "  ${BOLD}2. Choose history sources (nothing is read before this):${RESET}"
echo -e "     ${CYAN}penpal install onboard${RESET}"
echo ""
echo -e "  ${BOLD}3. Check dependencies & permissions:${RESET}"
echo -e "     ${CYAN}penpal doctor${RESET}"
echo ""
echo -e "  ${BOLD}4. Start monitoring:${RESET}"
echo -e "     ${CYAN}penpal start${RESET}"
echo ""
echo -e "  ${BOLD}5. View today's activity:${RESET}"
echo -e "     ${CYAN}penpal timeline --today${RESET}"
echo ""
echo "  Config:  $CONFIG_FILE"
echo "  Data:    $PENPAL_DIR/penpal.db"
echo "  Logs:    $PENPAL_DIR/penpal.log"
echo ""

# Run doctor using full path (PATH may not be reloaded yet in this shell)
echo -e "${BOLD}Running penpal doctor…${RESET}"
echo ""
"$BIN_DIR/penpal" doctor || true
