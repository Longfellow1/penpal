#!/usr/bin/env bash

penpal_is_legacy_keypulse_wrapper() {
  local path="$1"
  [[ -f "$path" && ! -L "$path" ]] || return 1

  grep -Eq '^[[:space:]]*(source|\.)[[:space:]].*\.keypulse/secrets\.env' "$path" && \
    grep -Eq '^[[:space:]]*exec[[:space:]].*\.keypulse/venv/bin/keypulse.*"\$@"[[:space:]]*$' "$path"
}

# A wrapper this script wrote earlier; refreshed in place, not reported as a custom file.
penpal_is_current_compat_wrapper() {
  local path="$1"
  [[ -f "$path" && ! -L "$path" ]] || return 1
  grep -Fq 'VENV_KEYPULSE="$HOME/.penpal/venv/bin/keypulse"' "$path"
}

penpal_install_compat_wrapper() {
  local path="$1"
  if [[ -e "$path" || -L "$path" ]]; then
    if ! penpal_is_legacy_keypulse_wrapper "$path" && ! penpal_is_current_compat_wrapper "$path"; then
      printf 'Warning: keeping custom file at %s; it was not recognized as the old KeyPulse wrapper.\n' "$path" >&2
      return 0
    fi
  fi

  mkdir -p "$(dirname "$path")"
  cat > "$path" <<'WRAPPER'
#!/usr/bin/env bash
if [[ -f "$HOME/.penpal/secrets.env" ]]; then
  source "$HOME/.penpal/secrets.env"
fi
VENV_KEYPULSE="$HOME/.penpal/venv/bin/keypulse"
if [[ -x "$VENV_KEYPULSE" ]]; then
  IFS= read -r VENV_SHEBANG < "$VENV_KEYPULSE" || true
  if [[ "$VENV_SHEBANG" != *"$HOME/.keypulse/venv/"* ]]; then
    exec "$VENV_KEYPULSE" "$@"
  fi
fi

# Public Homebrew installs do not use the developer venv. Resolve the alias next
# to the installed penpal command, avoiding this compatibility wrapper itself.
PENPAL_COMMAND="$(command -v penpal || true)"
if [[ -n "$PENPAL_COMMAND" ]]; then
  PENPAL_ALIAS="$(dirname "$PENPAL_COMMAND")/keypulse"
  if [[ -x "$PENPAL_ALIAS" && "$PENPAL_ALIAS" != "$HOME/.local/bin/keypulse" ]]; then
    exec "$PENPAL_ALIAS" "$@"
  fi
  printf 'Deprecated command: use `penpal` instead of `keypulse`.\n' >&2
  exec "$PENPAL_COMMAND" "$@"
fi

printf 'Deprecated command `keypulse` is unavailable; reinstall Penpal.\n' >&2
exit 127
WRAPPER
  chmod +x "$path"
}
