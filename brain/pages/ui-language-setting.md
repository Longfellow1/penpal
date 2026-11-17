---
id: ui-language-setting
title: "UI language is one setting ([app] language = auto|zh|en) shared by CLI and HUD; every user-facing Chinese string carries its English"
category: decision
status: active
tags: [i18n, hud, cli]
created: "2026-11-11T11:51:47"
updated: "2026-11-11T11:51:47"
---

<!-- compiled_truth -->
Owner 2026-11-11 asked for a zh/en switch in the HUD ("可以先支持英文").
- One setting: `[app] language = "auto" | "zh" | "en"` in config.toml, default auto. Priority: PENPAL_LANG > config > LANG/LC_ALL > system locale > en.
- An unknown value falls back to auto; it must never stop config from loading.
- Set it with `penpal config set-language` or the HUD Settings tab. Both call `write_app_language` + `i18n.set_lang()`, which only clears the cache.
- The HUD re-renders immediately: panel, status menu and approval window.
- Rule: every user-facing CJK string literal is `T(zh, en)` or a `(zh, en)` pair, resolved at render time, never at import time. The AST guard `tests/test_hud_viewmodel.py::test_hud_i18n_static_guard` enforces this for `penpal/hud`. Extending it to `penpal/cli` is the follow-up PR, together with the gaps: connect and daemon status are zh-only; sources, import and memory are en-only.
- English copy must fit the fixed 360px panel without shrinking the font, and keep the meaning, especially risk notes such as "filtering isn't perfect".
- MCP text addressed to AI tools is out of scope.


## Timeline

- time: 2026-11-11T11:51:47
  kind: decision
  summary: "Created this page: UI language is one setting ([app] language = auto|zh|en) shared by CLI and HUD; every user-facing Chinese string carries its English"
  source: owner 2026-11-11
  affects: [ui-language-setting]

- time: 2026-11-11T11:51:47
  kind: decision
  summary: Language switch design
  source: owner 2026-11-11
  affects: [ui-language-setting]
