from __future__ import annotations

from penpal.capabilities.base import Capability, CheckResult, HealthState, Signal
from penpal.capabilities.builtin._common import now_ts
from penpal.i18n import T
from penpal.runtime import evaluate

DISK_FULL_CODE = "capture_storage_disk_full"


class CaptureStorageCapability(Capability):
    """Surface capture's write state (CaptureManager pauses writes when the disk is full).

    Reads storage state from the shared daemon runtime record.
    """

    name = "capture_storage"
    level_when_failed = "err"
    label_when_failed = "采集写入失败"
    error_codes = {DISK_FULL_CODE, "capture_storage_error"}

    def precheck(self) -> CheckResult:
        return CheckResult(ok=True, code="ok")

    def _probe(self) -> CheckResult:
        components = evaluate().components
        storage = components.get("storage")
        dropped = components.get("storage_dropped_events")
        count = int(dropped.detail) if dropped and dropped.detail.isdecimal() else 0
        if (storage is None or storage.state != "storage_error") and not count:
            return CheckResult(ok=True, code="ok")
        reason = storage.detail if storage and storage.state == "storage_error" else "write_failed"
        suffix = T(f"；有 {count} 条记录未保存", f"; {count} events were not saved") if count else ""
        if reason == "disk_full":
            return CheckResult(
                ok=False,
                code=DISK_FULL_CODE,
                hint=T("磁盘已满，采集暂停写入；释放磁盘空间后会自动恢复", "Disk is full; capture resumes after space is freed") + suffix,
            )
        hint = T("采集写入失败，请运行 penpal doctor", "Capture storage failed; run penpal doctor")
        if storage is None or storage.state != "storage_error":
            hint = T("采集写入已恢复", "Capture storage has recovered")
        return CheckResult(ok=False, code="capture_storage_error", hint=hint + suffix)

    def monitor(self) -> HealthState:
        check = self._probe()
        return HealthState(ok=check.ok, code=check.code, last_checked=now_ts(), detail=check.hint)

    def diagnose(self, state: HealthState) -> Signal:
        if state.ok:
            return Signal(level="ok", label="正常", hint="", action=None)
        return Signal(
            level=self.level_when_failed,
            label=self.label_when_failed,
            hint=state.detail or "",
            action=None,
        )
