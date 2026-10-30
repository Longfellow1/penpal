from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

try:
    import AppKit  # type: ignore
except Exception:  # pragma: no cover - optional runtime dependency
    AppKit = None

try:
    import objc  # type: ignore
except Exception:  # pragma: no cover - optional runtime dependency
    class _ObjCShim:
        @staticmethod
        def IBAction(func):
            return func

        @staticmethod
        def selector(func, signature=None):
            return func

        @staticmethod
        def super(cls, obj):
            return super(cls, obj)

    objc = _ObjCShim()

from penpal.foundation.approval_store import ApprovalStore
from penpal.sources.discoverers import CandidateSource, discover_all_candidates
from penpal.sources.registry import discover_all


_DISCOVERER_ORDER = {
    "leveldb": 0,
    "sqlite": 1,
    "markdown_vault": 2,
    "json_files": 3,
    "jsonl": 4,
    "plist": 5,
}


@dataclass(frozen=True)
class PendingCandidateRow:
    candidate_id: str
    candidate: CandidateSource

    @property
    def app_name(self) -> str:
        return self.candidate.app_hint or "?"

    @property
    def path(self) -> str:
        return self.candidate.path

    @property
    def hint_tables_count(self) -> int:
        return len(self.candidate.hint_tables)

    @property
    def detail_text(self) -> str:
        hint_tables = ", ".join(self.candidate.hint_tables) if self.candidate.hint_tables else "(none)"
        hint_fields = ", ".join(self.candidate.hint_fields) if self.candidate.hint_fields else "(none)"
        return "\n".join(
            [
                f"id: {self.candidate_id}",
                f"discoverer: {self.candidate.discoverer}",
                f"shape: {self.candidate.shape}",
                f"schema_signature: {self.candidate.schema_signature}",
                f"hint_tables: {hint_tables}",
                f"hint_fields: {hint_fields}",
            ]
        )


def _flatten_candidates(candidates: dict[str, list[CandidateSource]]) -> list[CandidateSource]:
    flat: list[CandidateSource] = []
    for values in candidates.values():
        flat.extend(values)
    return sorted(flat, key=lambda candidate: (_DISCOVERER_ORDER.get(candidate.discoverer, 99), candidate.path))


def list_pending_candidates(
    *,
    approval_store: ApprovalStore | None = None,
    discover_all_fn: Callable[[], dict[str, list[Any]]] = discover_all,
    discover_candidates_fn: Callable[[set[str]], dict[str, list[CandidateSource]]] = discover_all_candidates,
) -> list[PendingCandidateRow]:
    store = approval_store or ApprovalStore()
    discovered = discover_all_fn()
    excluded = {
        str(getattr(instance, "locator", "") or "").strip()
        for instances in discovered.values()
        for instance in instances
        if str(getattr(instance, "locator", "") or "").strip()
    }

    candidates = _flatten_candidates(discover_candidates_fn(exclude_paths=excluded))
    rows: list[PendingCandidateRow] = []
    for candidate in candidates:
        candidate_id = store.candidate_id(candidate)
        if store.status(candidate_id) != "unknown":
            continue
        rows.append(PendingCandidateRow(candidate_id=candidate_id, candidate=candidate))
    return rows


def safe_pending_candidate_count(
    *,
    approval_store: ApprovalStore | None = None,
    discover_all_fn: Callable[[], dict[str, list[Any]]] = discover_all,
    discover_candidates_fn: Callable[[set[str]], dict[str, list[CandidateSource]]] = discover_all_candidates,
) -> tuple[int | None, str | None]:
    try:
        pending = list_pending_candidates(
            approval_store=approval_store,
            discover_all_fn=discover_all_fn,
            discover_candidates_fn=discover_candidates_fn,
        )
    except Exception as exc:  # pragma: no cover - defensive
        return (None, str(exc) or exc.__class__.__name__)
    return (len(pending), None)


class ApprovalDataController:
    def __init__(
        self,
        *,
        approval_store: ApprovalStore | None = None,
        list_pending_fn: Callable[..., list[PendingCandidateRow]] = list_pending_candidates,
    ) -> None:
        self._store = approval_store or ApprovalStore()
        self._list_pending_fn = list_pending_fn
        self.rows: list[PendingCandidateRow] = []
        self.last_error: str | None = None

    def load(self) -> tuple[list[PendingCandidateRow], str | None]:
        """Read pending rows without touching this controller, so it can run off the main thread."""
        try:
            return list(self._list_pending_fn(approval_store=self._store)), None
        except Exception as exc:  # pragma: no cover - defensive
            return [], str(exc) or exc.__class__.__name__

    def apply(self, loaded: tuple[list[PendingCandidateRow], str | None]) -> list[PendingCandidateRow]:
        rows, self.last_error = loaded
        self.rows = list(rows)
        return list(self.rows)

    def reload(self) -> list[PendingCandidateRow]:
        return self.apply(self.load())

    def count(self) -> int:
        return len(self.rows)

    def row_at(self, index: int) -> PendingCandidateRow | None:
        if index < 0 or index >= len(self.rows):
            return None
        return self.rows[index]

    def approve_at(self, index: int, *, note: str = "") -> bool:
        row = self.row_at(index)
        if row is None:
            return False
        self._store.approve(row.candidate, note=note)
        self.rows = [item for pos, item in enumerate(self.rows) if pos != index]
        return True

    def reject_at(self, index: int, *, reason: str = "user_choice") -> bool:
        row = self.row_at(index)
        if row is None:
            return False
        self._store.reject(row.candidate, reason=reason)
        self.rows = [item for pos, item in enumerate(self.rows) if pos != index]
        return True


if AppKit is not None:
    class ApprovalWindowController(AppKit.NSObject):
        def initWithStore_(self, approval_store: ApprovalStore | None):
            self = objc.super(ApprovalWindowController, self).init()
            if self is None:
                return None
            self.data = ApprovalDataController(approval_store=approval_store)
            self.window = None
            self.table_view = None
            self.summary_label = None
            self.detail_view = None
            self.details_expanded = False
            self.run_scan = None  # set by the HUD: runs discovery off the main thread
            return self

        def ensure_window(self) -> None:
            if self.window is not None:
                return

            frame = AppKit.NSMakeRect(120.0, 120.0, 840.0, 460.0)
            style = (
                AppKit.NSWindowStyleMaskTitled
                | AppKit.NSWindowStyleMaskClosable
                | AppKit.NSWindowStyleMaskMiniaturizable
                | AppKit.NSWindowStyleMaskResizable
            )
            self.window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                frame,
                style,
                AppKit.NSBackingStoreBuffered,
                False,
            )
            self.window.setReleasedWhenClosed_(False)
            self.window.setTitle_("候选审批")

            content = self.window.contentView()

            self.summary_label = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(20.0, 420.0, 780.0, 20.0))
            self.summary_label.setEditable_(False)
            self.summary_label.setBordered_(False)
            self.summary_label.setDrawsBackground_(False)
            self.summary_label.setStringValue_("待审批候选")
            content.addSubview_(self.summary_label)

            scroll = AppKit.NSScrollView.alloc().initWithFrame_(AppKit.NSMakeRect(20.0, 120.0, 800.0, 290.0))
            scroll.setHasVerticalScroller_(True)
            scroll.setHasHorizontalScroller_(False)

            self.table_view = AppKit.NSTableView.alloc().initWithFrame_(scroll.bounds())
            self.table_view.setDelegate_(self)
            self.table_view.setDataSource_(self)
            self.table_view.setUsesAlternatingRowBackgroundColors_(True)
            self.table_view.setAllowsEmptySelection_(False)

            col_app = AppKit.NSTableColumn.alloc().initWithIdentifier_("app")
            col_app.setTitle_("App")
            col_app.setWidth_(170.0)
            self.table_view.addTableColumn_(col_app)

            col_path = AppKit.NSTableColumn.alloc().initWithIdentifier_("path")
            col_path.setTitle_("Path")
            col_path.setWidth_(520.0)
            self.table_view.addTableColumn_(col_path)

            col_tables = AppKit.NSTableColumn.alloc().initWithIdentifier_("tables")
            col_tables.setTitle_("hint_tables")
            col_tables.setWidth_(110.0)
            self.table_view.addTableColumn_(col_tables)

            scroll.setDocumentView_(self.table_view)
            content.addSubview_(scroll)

            self.detail_view = AppKit.NSTextView.alloc().initWithFrame_(AppKit.NSMakeRect(20.0, 10.0, 800.0, 100.0))
            self.detail_view.setEditable_(False)
            self.detail_view.setString_("")
            self.detail_view.setHidden_(True)
            content.addSubview_(self.detail_view)

            button_specs = [
                ("批准", "approveSelected_", 20.0),
                ("拒绝", "rejectSelected_", 110.0),
                ("跳过", "skipSelected_", 200.0),
                ("查看详情", "toggleDetails_", 290.0),
                ("刷新", "reloadRows_", 410.0),
                ("关闭", "closeWindow_", 500.0),
            ]
            for title, action_name, x in button_specs:
                button = AppKit.NSButton.alloc().initWithFrame_(AppKit.NSMakeRect(x, 420.0, 100.0, 24.0))
                button.setTitle_(title)
                button.setBezelStyle_(AppKit.NSBezelStyleRounded)
                button.setTarget_(self)
                button.setAction_(objc.selector(getattr(self, action_name), signature=b"v@:@"))
                content.addSubview_(button)

        def showWindow_(self, _sender):
            self.ensure_window()
            self.reloadRows_(None)
            self.window.makeKeyAndOrderFront_(None)
            AppKit.NSApp.activateIgnoringOtherApps_(True)

        def _selected_row_index(self) -> int:
            if self.table_view is None:
                return -1
            return int(self.table_view.selectedRow())

        def _update_summary(self) -> None:
            if self.summary_label is None:
                return
            if self.data.last_error:
                self.summary_label.setStringValue_(f"候选读取失败: {self.data.last_error}")
                return
            self.summary_label.setStringValue_(f"待审批候选: {self.data.count()} 个")

        @objc.IBAction
        def reloadRows_(self, _sender):
            if self.run_scan is None:
                self.data.reload()
                self._show_rows()
                return
            if self.summary_label is not None:
                self.summary_label.setStringValue_("正在读取候选…")
            self.run_scan("approval_rows", self.data.load, lambda loaded: (self.data.apply(loaded), self._show_rows()))

        @objc.python_method
        def _show_rows(self) -> None:
            if self.table_view is not None:
                self.table_view.reloadData()
                if self.data.count() > 0:
                    self.table_view.selectRowIndexes_byExtendingSelection_(
                        AppKit.NSIndexSet.indexSetWithIndex_(0),
                        False,
                    )
            self._update_summary()
            self._refresh_detail()

        @objc.IBAction
        def approveSelected_(self, _sender):
            index = self._selected_row_index()
            if not self.data.approve_at(index):
                return
            self._after_action(index)

        @objc.IBAction
        def rejectSelected_(self, _sender):
            index = self._selected_row_index()
            if not self.data.reject_at(index):
                return
            self._after_action(index)

        @objc.IBAction
        def skipSelected_(self, _sender):
            index = self._selected_row_index()
            if index < 0 or self.table_view is None:
                return
            next_index = min(index + 1, max(self.data.count() - 1, 0))
            self.table_view.selectRowIndexes_byExtendingSelection_(
                AppKit.NSIndexSet.indexSetWithIndex_(next_index),
                False,
            )
            self._refresh_detail()

        @objc.IBAction
        def toggleDetails_(self, _sender):
            self.details_expanded = not self.details_expanded
            if self.detail_view is not None:
                self.detail_view.setHidden_(not self.details_expanded)
            self._refresh_detail()

        @objc.IBAction
        def closeWindow_(self, _sender):
            if self.window is not None:
                self.window.orderOut_(None)

        def _after_action(self, previous_index: int) -> None:
            if self.table_view is None:
                return
            self.table_view.reloadData()
            if self.data.count() > 0:
                next_index = min(previous_index, self.data.count() - 1)
                self.table_view.selectRowIndexes_byExtendingSelection_(
                    AppKit.NSIndexSet.indexSetWithIndex_(next_index),
                    False,
                )
            self._update_summary()
            self._refresh_detail()

        def _refresh_detail(self) -> None:
            if self.detail_view is None:
                return
            if not self.details_expanded:
                self.detail_view.setString_("")
                return
            row = self.data.row_at(self._selected_row_index())
            self.detail_view.setString_(row.detail_text if row is not None else "")

        def numberOfRowsInTableView_(self, _table_view):
            return self.data.count()

        def tableView_objectValueForTableColumn_row_(self, _table_view, table_column, row_index):
            row = self.data.row_at(int(row_index))
            if row is None:
                return ""
            identifier = str(table_column.identifier())
            if identifier == "app":
                return row.app_name
            if identifier == "path":
                return row.path
            if identifier == "tables":
                return str(row.hint_tables_count)
            return ""

        def tableViewSelectionDidChange_(self, _notification):
            self._refresh_detail()
else:
    class ApprovalWindowController:  # pragma: no cover - non-macOS fallback
        def __init__(self, *args, **kwargs):
            raise RuntimeError("ApprovalWindowController requires AppKit runtime")


__all__ = [
    "ApprovalDataController",
    "ApprovalWindowController",
    "PendingCandidateRow",
    "list_pending_candidates",
    "safe_pending_candidate_count",
]
