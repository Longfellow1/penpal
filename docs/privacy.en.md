# Penpal privacy boundary

[中文](privacy.md)

This document is for skeptical Mac users. Every claim names the code that implements it, and the test that proves it where one exists, so you can check instead of trusting the wording. The baseline is the current `main` branch.

Three things first:

1. **Filtering is best effort, not a guarantee.** Every "not recorded" and "masked" below is rule matching, and rules miss things. Do not rely on Penpal to guarantee that a password, a code or a private conversation never reaches the database.
2. **Data stays on your machine, but what an AI queries enters that AI's conversation.** If you grant a cloud AI (Claude, ChatGPT, ...), the snippets it retrieves may leave your computer.
3. **Keyboard snippets are on by default.** What is stored is filtered text, not a keystroke log, and one command turns it off.

Paths are relative to the repository root. "Test" names an existing test that demonstrates the behaviour.

## 1. What is stopped before anything is written

Every live capture event goes through one chain before the database (`_process_event` in `penpal/capture/manager.py`): global pause and source switches, exclusion list, domain exclusion, hygiene gate, policy rules, masking, write. There are three outcomes:

- **Dropped**: nothing is written, no trace remains.
- **Metadata only**: the app and the time are kept; text and window title are cleared and the reason is stamped as `text_dropped` in the metadata.
- **Masked**: the text is kept, sensitive parts are replaced by placeholders such as `[REDACTED]`.

| Case | Result | Code | Test |
|---|---|---|---|
| You ran `penpal pause`, or a source is switched off with `penpal source disable` | Dropped; events queued during a pause are not replayed after resume | `capture/capture_control.py` `source_writes_allowed`; comment in `manager.py` `_flush_loop` | `tests/test_capture_switches.py` |
| An app on the default exclusion list (section 2) | Dropped, including the fact that the app was used | `manager.py` `_is_blacklisted`; `capture/hygiene.py` `is_blacklisted_app` | `tests/test_blacklist.py` |
| A chat app you have not allowed (section 3) | Dropped | `manager.py` `_is_blacklisted`; `capture/chat_apps.py` | `tests/test_chat_app_scope.py`, `tests/test_issue26_capture_chain.py` |
| Browser-internal pages (`chrome://`, `about:`, ...) | Dropped | `hygiene.py` `is_internal_browser_page` | `tests/test_capture_hygiene.py` |
| Login screen, lock screen, system overlays (loginwindow, ...) | Window event dropped | `hygiene.py` `is_system_overlay` | `tests/test_capture_hygiene.py` |
| Text containing password / passwd / verification code / otp / 2fa / 密码 / 验证码 | That text is dropped | `hygiene.py` `_PRIVACY_RE` | `tests/test_capture_hygiene.py` |
| Window title with login-style words (sign in, 登录, 验证码, 1password, keychain, ...) | App and time only | `hygiene.py` `_LOGIN_KEYWORDS`, `is_login_context` | `tests/test_capture_hygiene.py` |
| Private / incognito window | App and time only | `hygiene.py` `is_private_window` | `tests/test_capture_hygiene.py` (`test_private_windows_are_metadata_only`) |
| Keyboard input in a terminal; screen text read from a terminal | Keyboard: app and time only. Screen text: text cleared | `hygiene.py` `is_terminal_app`; `manager.py` (`drop_terminal_text`, on by default) | `tests/test_capture_hygiene.py`, `tests/test_blacklist.py` |
| A domain you excluded (section 4) | URL event dropped; text typed, read or copied in that tab keeps only app and time | `manager.py`, `hygiene.py` `page_in_denied_domain` | `tests/test_issue26_capture_chain.py` |
| macOS Secure Input is on (password fields, ...) | Keyboard not recorded | `capture/watchers/keyboard_native.py` (`_is_secure_input_enabled`) | no dedicated test |
| Keys pressed with Cmd | Keyboard not recorded | `keyboard_native.py` | `tests/test_capture_hygiene.py` (`test_tap_keeps_space_and_enter_and_ignores_command_chords`) |

The login-word and password-word checks are substring / word-boundary matches. If a title lacks those words, or the text uses another term, they do not catch it.

## 2. Default exclusion list

Defined in `PrivacyConfig` in `penpal/config.py`:

- **Password managers and keychain**: 1Password, Bitwarden, Dashlane, LastPass, Keychain Access, Microsoft Authenticator.
- **Chat apps**: WeChat, WeCom, QQ, Telegram, Signal, WhatsApp, Messages, Slack, Discord, DingTalk, Feishu/Lark, LINE, KakaoTalk (plus a `com.tencent.*` wildcard).
- **Banking, brokerage, crypto wallets, health**: about 15 bundle ids (ICBC, Bank of China, Chase, Wells Fargo, Robinhood, MetaMask, Ledger, MyChart, Headspace, Calm, ...).
- **App-name substring match**: 微信, WeChat, 企业微信, 钉钉, DingTalk, 飞书, Lark, Feishu, 1Password, Keychain Access, 钥匙串, 密码 (any app whose name contains "密码" is excluded).

Limits you should know:

- Some of the banking-style bundle ids belong to phone apps. Whether a same-id app exists on the Mac was not verified for each one; they may never trigger on a Mac.
- These are built-in defaults. If you set `blacklist_bundle_ids`, `blacklist_app_names`, etc. under `[privacy]` in `~/.penpal/config.toml`, your list **replaces** that default list instead of extending it, so copy the defaults you still want (pydantic default semantics, see `config.py`).
- A set of domains is also excluded by default (`url_deny_hosts`): apple.com, appleid.apple.com, and Xiaohongshu, Weibo, Bilibili, Zhihu and Douyin. Section 4 explains how domain exclusion works.

## 3. Chat apps: off by default, Private once allowed

- Default: window, keyboard, screen text and clipboard from chat apps are all dropped (`manager.py` `_is_blacklisted`, `chat_apps.py`).
- Allow: `penpal privacy chat-apps allow Slack` (`penpal privacy chat-apps list` shows the list, `deny` reverts). It writes `[privacy] chat_apps_opt_in` in `config.toml` (`config.py`).
- After allowing, that app's data goes into the **Private** bucket (`store/buckets.py`), readable only by AIs granted Private; your own bucket rules take precedence over this default. Content dropped before you allowed the app is not recovered.
- Tests: `tests/test_chat_app_scope.py` (`test_opted_in_chat_app_is_private_and_hidden_from_work_personal_hosts`), `tests/test_issue26_capture_chain.py` (AT20).

Also: the WeChat message source (`penpal/sources/plugins/wechat.py`) currently performs local probing only; message reading is not implemented. It does not read or import WeChat messages, create Daily reports, or call a cloud LLM. `penpal source wechat authorize` only writes a local authorization marker. Tests: `tests/test_sources_wechat.py::test_wechat_source_discover_and_read_probe_only`, `tests/test_wechat_consent.py`.

## 4. Excluding apps and domains yourself

- **Apps**: set `blacklist_app_names` / `blacklist_bundle_ids` under `[privacy]` in `config.toml` (a match drops the event), or use `penpal rules add --scope-type app --scope-value <name> --mode deny`.
- **Domains**: use `penpal privacy block-site <host>` to block a website, `penpal privacy unblock-site <host>` to unblock it, and `penpal privacy blocked-sites` to list blocked hosts. The command removes the URL scheme, path, port and leading `www.`, then lowercases the host. Matching covers that host and all its subdomains (`hygiene.py` `host_matches_denylist`).
- New live capture events use the updated list within about one minute; no manual restart is needed. Existing data is not deleted by this setting.
- Domain exclusion now covers three places:
  1. Live URL events: dropped.
  2. **Browser-history import**: matching visits are not stored (`sources/importer.py`, `sources/registry.py`). Test: `tests/test_issue26_capture_chain.py::test_at18_browser_history_from_excluded_domain_is_not_stored`.
  3. **Text typed, read or copied in that tab, and the foreground window title**: only app and time are kept (`denied_domain`; the window title and the `primary_title` copy in metadata are cleared too). Tests: `test_typing_in_excluded_domain_tab_is_not_stored_as_content` and `test_window_title_on_excluded_domain_tab_is_not_stored` in the same file.

Item 3 has a precondition and gaps you must know about:

- It learns which page you are on from the **browser URL watcher. Without that watcher running, Penpal does not know the site and this protection does not apply**; text is stored as usual (`test_unknown_page_is_not_blocked`). The code default for `WatchersConfig.browser_url` is off (`config.py`), but the `~/.penpal/config.toml` template that Penpal writes at install turns it on (`_DEFAULT_USER_CONFIG` in `cli.py`). So a normal install has it on; confirm with `penpal source list`.
- The URL is polled every 3 seconds and reported only when it changes, so right after you switch tabs there can be a delay of about one poll interval, during which text is judged against the previous page.
- Window-switch events use the same "last known page" check, with two gaps. First, when you switch to a new tab the window event can arrive before the URL watcher reports it, so for up to one poll interval it is judged against the previous page: switching from an allowed site into an excluded one can store that title once. Second, history migration (`store/migration_v24.py`) only re-checks text, not window rows already stored.

## 5. Keyboard

Decision: D35 (2026-10-24; default-on dates from 2026-10-14). It **reverses** the older wording "off by default" and "tagged, excluded from AI search by default"; see D35 in `docs/Penpal_V2.md`.

**What is recorded**

- Not per keystroke. Key presses are assembled in memory into text; a chunk closes after 2 seconds of silence or 20 seconds of continuous typing, backspace is applied and control characters are removed (`capture/watchers/keyboard_chunk.py`; `KeyboardChunkConfig` in `config.py`).
- The database holds only the filtered chunk, its app and its time (`tests/test_purge_cascade.py::test_keyboard_chunk_rows_are_deleted_with_their_derived_data` shows the stored row shape).

**Filter rules** (`capture_gate` in `hygiene.py`, keyboard part, in order):

| Rule | Action |
|---|---|
| Contains password / otp / 2fa / 密码 / 验证码 etc. | Dropped |
| Window title has login-style words | App and time only |
| Typed in a terminal app | App and time only |
| Fewer than 5 characters (except `y`, `n`, `ok`, `yes`, `no`, `嗯`, `好`, `对`, `是`, `不`; two or more Chinese characters are also kept) | Dropped |
| 8+ characters with Shannon entropy <= 2.0 (e.g. `aaaaaaaa`, `abababab`) | Dropped |
| No CJK text and a longest unbroken run >= 12 characters of flat lowercase (looks like IME composition, a hash or a token) | Dropped |
| ASCII-only, longer than 12 characters, no spaces, >= 70% letters, no sentence punctuation, not code-like | Dropped |
| Survives all of the above | Masked per section 6, truncated to 2000 characters |

Tests: `tests/test_capture_hygiene.py` (`test_keyboard_gate`, `test_keyboard_in_terminal_is_metadata_only`, `test_english_sentence_through_buffer_passes_gate_and_pinyin_does_not`).

**Switch and notice**

- On by default. A one-time risk notice is shown at install/init (`capture/keyboard_notice.py`, test `tests/test_keyboard_notice.py`). It says filtering is best effort, sensitive text may be captured, and AIs read only the buckets they were granted.
- One command turns it off: `penpal source disable keyboard_chunk` (`penpal source enable keyboard_chunk` turns it back on). Once off, the listener is quiet and nothing is written (`manager.py` `_sync_source_switches`).
- Chunks already stored do not vanish when you turn it off. To delete them: `penpal purge --source keyboard_chunk` (section 10).

**Can an AI search it (D35)**

- Yes, like any other source, and only within the buckets that AI was granted. Chunks are bucketed by the app they were typed in: editors and terminals are Work, apps with no preset are Personal (`store/buckets.py`; tests `tests/test_buckets_grants.py`, `tests/test_capture_mcp.py::test_capture_streams_are_searchable_with_kind_and_snippet`).
- A new grant covers Work only (`DEFAULT_BUCKETS` in `mcp/auth.py`).
- Only two kinds of rows are hidden from search: garbled keyboard rows stored before the schema v24 fix (`pre_fix_keyboard`), and AI answers that merely echo Penpal's own output (`via_penpal`). The second is about echoes, not keyboard (`search/engine.py`).

**Chinese IME**: coverage is currently reliable for English only. Pinyin composition is dropped, and capture of committed Chinese text is experimental; progress is in issue #64. Do not assume your Chinese typing is recorded, and do not assume it is not.

## 6. Field-level masking

On write, text, window titles and metadata are masked field by field (`penpal/privacy/desensitizer.py`, `detectors.py`; called from `_process_event` in `manager.py`).

| Content | Treatment | Note |
|---|---|---|
| Email | `alice@example.com` becomes `***@example.com` (domain kept) | service accounts such as `git@host` are left alone |
| Mainland-China mobile number (11 digits) | `[REDACTED]` | |
| National ID number (18 digits) | `[ID_CARD]` | only if the check digit is valid |
| Card number (16-19 digits) | `[BANK_CARD]` | only if the Luhn check passes, to spare order ids and timestamps |
| `password=...`, `token: ...`, `api_key=...`, `secret=...` | `[REDACTED]` | the whole key-value up to whitespace |
| Bearer tokens, JWTs, AWS keys (`AKIA...`), `sk-...` keys, GitHub tokens, Slack tokens, `?token=`-style URL parameters, private-key headers, UUID v4 | `[REDACTED]` | |
| Lines starting with `sudo`, `export X=...`, `brew/npm/pip install ...` etc. | `[REDACTED_SHELL]` | keeps credentials embedded in commands out |
| Penpal's own MCP tokens (`kp_...`) | `[REDACTED]` | so they cannot reappear via ingested session transcripts |

Tests: `tests/test_privacy_detectors.py`, `tests/test_capture_hygiene.py` (card and ID checks), `tests/test_auth_audit.py::test_keypulse_token_in_ingested_text_is_redacted`.

Three limits:

- **Live capture** (window, clipboard, keyboard, screen text) goes through every rule above. **Imported sources** (Claude Code/Codex sessions, shell history, Git, Markdown, Spotlight) get only credential-class masking at ingest (`sources/sink.py`, `store/repository.py`): emails, phone numbers, ID numbers and card numbers in imported text are not replaced there (browser-history URLs are the exception and get the full pass). Emails are masked again when results are returned to an AI (`mcp/tools.py`, controlled by `privacy.redact_emails`, on by default); phone, ID and card numbers in imported content have no such second layer.
- Every write path also runs a credential-class pass just before the row is stored (`insert_raw_event` in `store/repository.py`), and results are masked once more when returned.
- All of this is regex. It has no understanding of meaning; "my passphrase is hunter2" with no keyword gets through.

## 7. While the camera is on

- When any program is using the camera, Penpal pauses **screen-text (accessibility text, `ax_text`)** capture and resumes when the camera is released (`capture/camera_monitor.py` reads the system log for camera state; `_start_camera_monitor` in `manager.py`; setting `privacy.camera_scene_pause`, on by default). Test: `tests/test_camera_monitor.py`.
- Two caveats: only screen text is paused; **keyboard, clipboard and window switching continue**. And the screen-text source itself is off by default (`ax_text: False` in `config.py`), so for most users this rule is a safety net.

## 8. Private browsing

A window whose title or URL contains incognito, 无痕, InPrivate, Private Browsing or 隐私浏览 records only app and time; text and title are cleared (`hygiene.py`). The test covers Chrome incognito, Chinese "无痕", Edge InPrivate, "Private Browsing" and Safari private titles (`tests/test_capture_hygiene.py::test_private_windows_are_metadata_only`). Detection is by wording in the title/URL, so a browser that changes its wording can slip through.

## 9. What an AI host can see

| Claim | Evidence |
|---|---|
| One token per AI (host); only its hash is stored, the plaintext is shown once at creation | `mcp/auth.py` `create_grant`, `hash_token`; `tests/test_auth_audit.py::test_g5_no_plaintext_token_or_hash_in_db_wal_logs_audit_or_cli_output` |
| Grants are scoped by bucket (Work / Personal / Private / Financial), default Work only; sources and expiry can also be limited | `mcp/auth.py` `DEFAULT_BUCKETS`, `Grant`; `tests/test_buckets_grants.py` |
| Work includes all conversations from enabled Claude Code / Codex sources; a project bucket rule keyed by folder name plus `penpal rules rebucket` reclassifies stored rows to Personal | `store/buckets.py`, `sources/plugins/claude_code.py`, `sources/plugins/codex_cli.py`, `penpal rules projects`; `tests/test_buckets_grants.py::test_project_rule_precedes_app_source_and_default_and_matches_exactly`, `test_project_rule_rebucket_applies_to_legacy_project_dir_metadata` |
| A call needs all of: a valid token, a client name matching the grant, and that client on your approved list | `mcp/auth.py` `check_access`; `tests/test_auth_audit.py` |
| Revoke is immediate (nothing is cached); `rotate` issues a new token and the old one stops at once | `revoke_grants`, `rotate_grant`; `tests/test_auth_audit.py::test_revoke_is_immediate_and_removes_whitelist`, `test_rotate_keeps_grant_and_scope_and_kills_old_token` |
| The audit log records who (client name), when, which tool, allow or deny, the granted bucket names, and how many results an allowed call returned. **It does not record query text, snippets or tokens** | `mcp/audit.py` (the table has no column that could hold them); `penpal mcp audit`; `tests/test_auth_audit.py` |
| Denied calls are audited too, and the caller gets an identical denial body that reveals no reason | `tests/test_auth_audit.py::test_at15_every_denial_has_identical_body_and_an_audit_row` |
| Text returned to the AI gets a credential pass and email masking again | `mcp/tools.py` `_redact_text` |
| There are four tools: `search_context`, `get_timeline`, `get_evidence`, `rate_result` | `mcp/server.py` |

Limits:

- **Read-only, with one exception**: `rate_result` writes a rating record on your machine (the verdict your AI sends, plus an optional note and query text, length-limited). It needs only read permission, can only rate results that client was actually shown, and `penpal purge` removes it with the related events. A grant can name a `write` operation, but no tool requires it today.
- **Client identity is self-declared.** Over stdio Penpal cannot prove who the peer process is; `PENPAL_CLIENT` is just a name in the host's config, and the real credential is the token. A process that can read your host config gets both (header comment in `mcp/auth.py`). Protect that file.
- **What is retrieved enters that AI's conversation.** If the AI is a cloud service (Claude, ChatGPT, a Cursor backend, ...), it may leave your computer. Penpal cannot control what the host does with it afterwards. Think before granting a bucket.
- "AI" here means a host you authorised. Penpal's core embeds no model and calls none itself.

## 10. Where data lives and how to delete it

- **Location**: `~/.penpal/` holds the database `penpal.db`, the log `penpal.log` and the config `config.toml` (`AppConfig` in `config.py`, `utils/paths.py`).
- **No telemetry**: the `penpal/` source contains no network code using `requests`, `httpx`, `urllib.request`, `http.client` or `aiohttp`, and no analytics SDK. That is a code search, not a packet capture; downloading Penpal itself (e.g. via Homebrew) is outside this claim.
- **No automatic deletion**: starting or stopping never deletes data, and the old retention setting is ignored. Test: `tests/test_d32_no_auto_delete.py` (D32). Deletion happens only when you run it.
- **Purge**: `penpal purge --source <source>` / `--app <app>` / `--today` / `--last-hours N`; add `--dry-run` to see counts first (`purge` in `cli.py`, `services/purge.py`). In one transaction it removes the raw rows, the full-text index, evidence, the records of results returned to AIs, rating records, search documents for clipboard/manual rows, and the sessions that depended on them (a session that keeps other events is rebuilt so no deleted text survives). A call with no selector is refused, not treated as "delete everything", and any failure rolls everything back. Test: `tests/test_purge_cascade.py`.
- Whether the log file can contain content fragments was not checked line by line; the code convention is that logs carry reason counts, not content (header comment in `hygiene.py`).

## 11. What is not promised

- That any filter will always catch what it targets. Rules use keywords, regexes and check digits with no semantic understanding; Chinese phrasing, dialects, renamed apps and unusual sites can slip through.
- That other programs cannot read `~/.penpal/`. The database is not separately encrypted; protection comes from your macOS account permissions and disk encryption (FileVault).
- What a cloud AI does with what it retrieves.
- Chinese IME capture behaviour (issue #64, experimental).
- Support beyond macOS.

If you find behaviour that disagrees with this document, please open an issue; for security problems see [SECURITY.md](../SECURITY.md).
