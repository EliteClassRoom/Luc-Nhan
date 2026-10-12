from __future__ import annotations

# Run only in IDA GUI against an empty database: ida.exe -t -A -S".../benchmark_qt.py".
# Synthetic catalog; no provider calls, credentials, binaries, or database mutations.
import ctypes
import importlib
import json
import math
import os
import platform
import sys
import time
import traceback
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
qt = importlib.import_module("lucnhan.ui.qt_compat")
core = importlib.import_module("PySide6.QtCore")
binding = importlib.import_module("PySide6")
ida_pro = importlib.import_module("ida_pro")
ida_kernwin = importlib.import_module("ida_kernwin")
app = qt.QApplication.instance()
assert app is not None and ida_kernwin.is_idaq()


class ProcessMemory(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong)] + [
        (name, ctypes.c_size_t)
        for name in (
            "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
            "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
            "PagefileUsage", "PeakPagefileUsage",
        )
    ]


kernel = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)
kernel.GetCurrentProcess.restype = ctypes.c_void_p
psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(ProcessMemory), ctypes.c_ulong]
psapi.GetProcessMemoryInfo.restype = ctypes.c_int


def memory_mib() -> float:
    counters = ProcessMemory()
    counters.cb = ctypes.sizeof(counters)
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return counters.WorkingSetSize / 1048576


def summarize(values: list[float]) -> dict[str, object]:
    ordered = sorted(values)
    return {
        "samples": len(values), "median_ms": ordered[len(ordered) // 2],
        "p95_ms": ordered[math.ceil(len(ordered) * 0.95) - 1],
        "max_ms": ordered[-1], "raw_ms": values,
    }


def measure(action: Callable[[], None], samples: int = 100) -> dict[str, object]:
    values = []
    for _ in range(samples):
        start = time.perf_counter_ns()
        action()
        app.processEvents()
        values.append((time.perf_counter_ns() - start) / 1e6)
    return summarize(values)


class Picker(qt.QFrame):
    def __init__(self, owner: qt.QWidget, providers: int, count: int) -> None:
        super().__init__(owner, core.Qt.WindowType.Popup)
        self.resize(360, 420)
        layout = qt.QVBoxLayout(self)
        self.search = qt.QLineEdit(self)
        self.search.setPlaceholderText("Search provider or model")
        layout.addWidget(self.search)
        self.tree = qt.QTreeWidget(self)
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        layout.addWidget(self.tree)
        self.thinking = qt.QComboBox(self)
        self.thinking.addItems(["Low", "Medium", "High"])
        layout.addWidget(self.thinking)
        self.groups: list[tuple[qt.QTreeWidgetItem, str, list[tuple[qt.QTreeWidgetItem, str]]]] = []
        for provider in range(providers):
            name = f"Connection {provider:02d}"
            group = qt.QTreeWidgetItem(self.tree, [name])
            rows = []
            for model in range(provider, count, providers):
                model_name = f"model-{model:05d}"
                item = qt.QTreeWidgetItem(group, [model_name])
                item.setData(0, core.Qt.ItemDataRole.UserRole, (name, model_name))
                rows.append((item, f"{name} {model_name}".casefold()))
            group.setExpanded(True)
            self.groups.append((group, name.casefold(), rows))
        self.search.textChanged.connect(self.filter_catalog)

    def filter_catalog(self, query: str) -> None:
        needle = query.casefold()
        self.tree.setUpdatesEnabled(False)
        try:
            for group, _, rows in self.groups:
                any_match = False
                for item, label in rows:
                    match = needle in label
                    item.setHidden(not match)
                    any_match |= match
                group.setHidden(not any_match)
        finally:
            self.tree.setUpdatesEnabled(True)


window = qt.QWidget()
window.setWindowTitle("Luc Nhan — isolated native picker benchmark (empty IDB)")
window.resize(460, 710)
layout = qt.QVBoxLayout(window)
layout.addWidget(qt.QLabel("Synthetic workspace; no provider or binary access", window))
layout.addWidget(qt.QLabel("Tool activity area — existing chat renderer NOT benchmarked", window), 1)
draft = qt.QPlainTextEdit(window)
draft.setPlainText("Draft retained across model and thinking changes.")
draft.setMaximumHeight(110)
layout.addWidget(draft)
footer = qt.QLabel("Connection 00 / model-00000 · High", window)
layout.addWidget(footer)
window.show()
app.processEvents()
result: dict[str, object] = {
    "scope": "native grouped/searchable model-thinking prototype inside IDA GUI; not full plugin redesign",
    "timing": "synchronous operation plus QApplication.processEvents; not input-to-screen compositor latency",
    "environment": {
        "python": sys.version, "executable": sys.executable,
        "configured_python": os.environ.get("IDAPYTHON_VENV_EXECUTABLE"),
        "qt": core.qVersion(), "pyside": binding.__version__, "os": platform.platform(),
        "logical_cpus": os.cpu_count(), "platform": app.platformName(),
        "window_logical_pixels": [window.width(), window.height()],
        "device_pixel_ratio": window.devicePixelRatioF(),
    },
    "baseline_working_set_mib": memory_mib(), "cases": [],
}
current_picker: Picker | None = None
idle_start: tuple[float, float] | None = None


def guarded(action: Callable[[], None]) -> None:
    try:
        action()
    except Exception:
        result["error"] = traceback.format_exc()
        finish()


def later(delay: int, action: Callable[[], None]) -> None:
    core.QTimer.singleShot(delay, lambda: guarded(action))


def finish() -> None:
    (ROOT / "design-preview" / "benchmark-native.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    if current_picker is not None:
        current_picker.close()
    window.close()
    ida_pro.qexit(1 if "error" in result else 0)


def begin_idle() -> None:
    global idle_start
    idle_start = (time.perf_counter(), time.process_time())
    later(5000, end_baseline_idle)


def end_baseline_idle() -> None:
    assert idle_start is not None
    result["baseline_idle"] = idle_result()
    run_case(0)


def idle_result() -> dict[str, float]:
    assert idle_start is not None
    seconds = time.perf_counter() - idle_start[0]
    cpu = time.process_time() - idle_start[1]
    return {"wall_seconds": seconds, "process_cpu_seconds": cpu, "one_core_percent": cpu / seconds * 100}


def run_case(index: int) -> None:
    global current_picker
    cases = [(3, 60), (10, 1000), (20, 5000)]
    providers, count = cases[index]
    before = memory_mib()
    start = time.perf_counter_ns()
    picker = Picker(window, providers, count)
    current_picker = picker
    picker.show()
    app.processEvents()
    assert picker.isVisible()
    cold = (time.perf_counter_ns() - start) / 1e6
    after = memory_mib()

    def reopen() -> None:
        picker.hide()
        picker.show()

    for _ in range(10):
        reopen()
        app.processEvents()
    opening = measure(reopen)
    queries = ["model-000", "Connection 01", "not-found", ""]
    cursor = 0

    def filtering() -> None:
        nonlocal cursor
        picker.search.setText(queries[cursor % len(queries)])
        cursor += 1

    filtering_result = measure(filtering)
    picker.search.setText("not-found")
    assert all(group.isHidden() for group, _, _ in picker.groups)
    picker.search.setText("Connection 01")
    assert sum(not group.isHidden() for group, _, _ in picker.groups) == 1
    picker.search.clear()
    assert all(not item.isHidden() for _, _, rows in picker.groups for item, _ in rows)
    cursor = 0

    def selection() -> None:
        nonlocal cursor
        group, _, rows = picker.groups[cursor % providers]
        item, _ = rows[cursor % len(rows)]
        picker.tree.setCurrentItem(item)
        picker.thinking.setCurrentIndex(cursor % 3)
        connection, model = item.data(0, core.Qt.ItemDataRole.UserRole)
        footer.setText(f"{connection} / {model} · {picker.thinking.currentText()}")
        cursor += 1

    selecting = measure(selection)
    assert draft.toPlainText() == "Draft retained across model and thinking changes."
    result["cases"].append({
        "providers": providers, "models": count, "cold_create_and_show_ms": cold,
        "working_set_delta_mib": after - before, "working_set_after_mib": after,
        "warm_open": opening, "search": filtering_result, "select_model_thinking": selecting,
        "checks": {"zero_results": True, "provider_filter": True, "restore_all": True, "draft_retained": True},
    })
    if index == 0:
        picker.grab().save(str(ROOT / "design-preview" / "benchmark-native-picker.png"))
    if index < len(cases) - 1:
        picker.close()
        picker.deleteLater()
        app.processEvents()
        later(500, lambda: run_case(index + 1))
    else:
        later(1000, begin_popup_idle)


def begin_popup_idle() -> None:
    global idle_start
    idle_start = (time.perf_counter(), time.process_time())
    later(5000, end_popup_idle)


def end_popup_idle() -> None:
    result["popup_idle"] = idle_result()
    result["paired_idle"] = []
    later(1000, lambda: begin_paired_idle(0))


def begin_paired_idle(index: int) -> None:
    assert current_picker is not None
    if index % 2:
        current_picker.show()
    else:
        current_picker.hide()
    app.processEvents()
    later(1000, lambda: start_paired_idle(index))


def start_paired_idle(index: int) -> None:
    global idle_start
    idle_start = (time.perf_counter(), time.process_time())
    later(5000, lambda: end_paired_idle(index))


def end_paired_idle(index: int) -> None:
    result["paired_idle"].append({
        "state": "open" if index % 2 else "closed", "models": 5000, **idle_result(),
    })
    if index < 5:
        later(200, lambda: begin_paired_idle(index + 1))
    else:
        finish()


later(1000, begin_idle)
