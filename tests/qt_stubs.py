"""Shared PySide6 stub injection for UI tests.

Must be called BEFORE importing any rikugan.ui module. Example::

    from tests.qt_stubs import ensure_pyside6_stubs
    ensure_pyside6_stubs()
    from rikugan.ui.some_module import ...
"""

from __future__ import annotations

import sys
import types
from enum import IntFlag

_installed = False

# Global focus tracking for tests. Real Qt routes focus through the
# platform window manager; the stub has no event loop so it uses a
# single-slot dict keyed by ``"current"``. ``setFocus`` overwrites the
# slot; ``hasFocus`` checks it against the widget instance.
_STUB_FOCUS_STATE: dict = {"current": None}
# Default focus policy for stubbed widgets. Mirrors real Qt's
# ``QWidget.focusPolicy`` default (``NoFocus``) so a widget that does
# NOT call ``setFocusPolicy`` still reports the real default under
# ``focusPolicy()``. Production code that wants the button in the tab
# chain must call ``setFocusPolicy(StrongFocus)`` explicitly — the
# tests then pin that explicit pin.
_DEFAULT_FOCUS_POLICY: object = None  # set by ``ensure_pyside6_stubs`` once ``Qt`` exists.


def _qt_class(name: str) -> type:
    """Create a minimal stubbed Qt class that supports subclassing.

    Provides common QWidget / QLayout methods as no-ops so that
    constructor chains (super().__init__ → setObjectName → setStyleSheet)
    succeed without a real Qt runtime.
    """

    def _noop(self, *a, **k):
        return None

    def _visible_getter(self):
        return getattr(self, "_visible", True)

    def _visible_setter(self, val):
        self._visible = val

    def _checked_getter(self):
        return getattr(self, "_checked", False)

    def _checked_setter(self, val):
        self._checked = bool(val)

    def _hidden_getter(self):
        return not getattr(self, "_visible", True)

    def _text_getter(self):
        return getattr(self, "_text", "")

    def _text_setter(self, val):
        self._text = val
        # Fire ``textChanged`` so listeners (search-driven widgets)
        # observe the update — this matches PySide6's automatic emit
        # on QLineEdit. We only fire when the value actually changes
        # so tests that re-set the same text don't double-process.
        signal = getattr(self, "_signal_textChanged", None)
        if signal is not None and getattr(self, "_text_prev", None) != val:
            self._text_prev = val
            try:
                signal.emit(val)
            except Exception:
                pass

    def _plain_text_getter(self):
        return getattr(self, "_plain_text", "")

    def _plain_text_setter(self, val):
        self._plain_text = val

    def _min_h_setter(self, val):
        self._min_h = int(val)

    def _min_h_getter(self):
        return getattr(self, "_min_h", 0)

    def _max_h_setter(self, val):
        self._max_h = int(val)

    def _max_h_getter(self):
        return getattr(self, "_max_h", 0)

    def _fixed_h_setter(self, val):
        self._fixed_h = int(val)

    def _fixed_h_getter(self):
        return getattr(self, "_fixed_h", 0)

    def _font_metrics(self, *a, **k):
        """Return a stub QFontMetrics whose ``lineSpacing()`` is a fixed value.

        Used by code editor sizing in tool widgets. Production code calls
        ``self._code_edit.fontMetrics().lineSpacing()`` to compute visible
        row height; the stub returns a sensible default so widget
        construction does not blow up.
        """

        class _FontMetrics:
            def lineSpacing(self):
                return 14

        return _FontMetrics()

    def _set_text_format(self, fmt) -> None:
        # Track the format so tests can verify ``PlainText`` was forced
        # on user-facing titles. Real Qt exposes this via QLabel.textFormat().
        self._textFormat = int(fmt)

    def _text_format_getter(self):
        # Default mirrors real Qt's ``AutoText`` (2). The setter is
        # added next to this getter so tests can assert the panel
        # forced ``PlainText`` (0) on user-facing titles.
        return getattr(self, "_textFormat", 2)

    def _set_word_wrap(self, val) -> None:
        self._wordWrap = bool(val)

    def _word_wrap_getter(self):
        return getattr(self, "_wordWrap", False)

    def _set_maximum_width(self, val) -> None:
        self._maximumWidth = int(val)

    def _maximum_width_getter(self):
        # 16777215 == QWIDGETSIZE_MAX in real Qt; tests assert values
        # <= 320 once the panel constrains its content.
        return getattr(self, "_maximumWidth", 16777215)

    def _set_horizontal_scrollbar_policy(self, policy) -> None:
        self._horizontalScrollBarPolicy = policy

    def _horizontal_scrollbar_policy_getter(self):
        # Default mirrors real Qt's default (ScrollBarAsNeeded = 0).
        return getattr(self, "_horizontalScrollBarPolicy", 0)

    def _set_clear_button_enabled(self, val) -> None:
        self._clearButtonEnabled = bool(val)

    def _clear_button_enabled_getter(self):
        return getattr(self, "_clearButtonEnabled", False)

    def _set_placeholder_text(self, text: str) -> None:
        self._placeholderText = str(text)

    def _placeholder_text_getter(self):
        return getattr(self, "_placeholderText", "")

    def _set_stylesheet(self, qss: str) -> None:
        self._styleSheet = str(qss)

    def _stylesheet_getter(self):
        return getattr(self, "_styleSheet", "")

    def _set_tooltip(self, value: object) -> None:
        self._tooltip = "" if value is None else str(value)

    def _tooltip_getter(self) -> str:
        return getattr(self, "_tooltip", "")

    def _set_accessible_name(self, value: object) -> None:
        self._accessible_name = "" if value is None else str(value)

    def _accessible_name_getter(self) -> str:
        return getattr(self, "_accessible_name", "")

    def _set_enabled(self, value: object) -> None:
        self._enabled = bool(value)

    def _is_enabled(self) -> bool:
        return getattr(self, "_enabled", True)

    def _set_focus(self, reason=None) -> None:
        # Record the widget as the focused one globally so
        # ``hasFocus`` can be queried deterministically. The real Qt
        # engine routes focus through the platform window manager; the
        # stub uses a single-slot model since no event loop runs in
        # tests.
        self._has_focus = True
        _STUB_FOCUS_STATE["current"] = self

    def _has_focus(self) -> bool:
        return getattr(self, "_has_focus", False)

    def _set_focus_policy(self, policy: object) -> None:
        # Real PySide6 enumerates ``Qt.FocusPolicy`` values; the stub
        # only stores the policy object so tests can assert equality
        # against ``Qt.FocusPolicy.StrongFocus``.
        self._focusPolicy = policy

    def _focus_policy(self) -> object:
        # Default mirrors real Qt's ``QWidget.focusPolicy`` default
        # (``NoFocus``); tests pin specific policies via
        # ``assertEqual(policy, Qt.FocusPolicy.StrongFocus)`` after
        # the production code calls ``setFocusPolicy``.
        return getattr(self, "_focusPolicy", _DEFAULT_FOCUS_POLICY)

    def _set_minimum_size(self, w=None, h=None, *args) -> None:
        # ``setMinimumSize(w, h)`` and ``setMinimumSize(QSize)`` both
        # route here. Real Qt returns void; the stub persists the
        # values so ``minimumSize`` can be asserted against.
        if w is None:
            return
        if hasattr(w, "width") and hasattr(w, "height") and h is None:
            self._minimumWidth = int(w.width())
            self._minimumHeight = int(w.height())
            return
        try:
            self._minimumWidth = int(w)
            self._minimumHeight = int(h) if h is not None else 0
        except (TypeError, ValueError):
            return

    def _minimum_size(self):
        # Returns a QSize-like object whose ``width``/``height`` are
        # the minimums last set. Tests assert these are >= 24 for the
        # delete button to satisfy the WCAG 2.5.5 24x24 target size.
        class _StubSize:
            def __init__(self, w: int, h: int) -> None:
                self._w = w
                self._h = h

            def width(self) -> int:
                return self._w

            def height(self) -> int:
                return self._h

        return _StubSize(getattr(self, "_minimumWidth", 0), getattr(self, "_minimumHeight", 0))

    def _layout_getter(self):
        """Return the QLayout attached via ``setLayout``.

        tool_widgets walks its own layout via ``layout().itemAt(i).widget()``
        to look up the code section, so the stub must preserve the set layout.
        """
        return getattr(self, "_layout", None)

    def _layout_setter(self, layout):
        self._layout = layout

    def _layout_add_widget(self, w, *a, **k):
        items = getattr(self, "_items", None)
        if items is None:
            items = []
            self._items = items
        items.append(w)

    def _layout_add_widget_with_stretch(self, w, stretch=0, *a, **k):
        # Variant of ``addWidget`` that accepts the optional ``stretch``
        # integer. The default ``_layout_add_widget`` only consumes
        # one positional argument; ``addWidget(widget, 1, alignment)``
        # fails on the stub otherwise.
        return self._layout_add_widget(w, *a, **k)

    def _layout_add_layout(self, layout, *a, **k):
        items = getattr(self, "_items", None)
        if items is None:
            items = []
            self._items = items
        items.append(layout)

    def _layout_add_stretch(self, *a, **k):
        return None

    def _layout_item_at(self, index):
        items = getattr(self, "_items", [])

        class _Item:
            def __init__(self, w):
                self._w = w

            def widget(self):
                return self._w

            def layout(self):
                return None

        if index < 0 or index >= len(items):
            return _Item(None)
        return _Item(items[index])

    class _HeaderStub:
        """Stub for QHeaderView (returned by QTableWidget.verticalHeader()/.horizontalHeader()).

        Any real Qt method call becomes a no-op. Tests that need to
        assert header behavior can set sentinel attributes on this
        stub (e.g. ``header.visible = True``).
        """

        def setVisible(self, visible: bool) -> None:
            self.visible = visible

        def setSectionResizeMode(self, *args, **kwargs) -> None:
            self.resize_mode = args[0] if args else None

    attrs = {
        "__init__": _noop,
        # QWidget common
        "setObjectName": _noop,
        "setStyleSheet": _set_stylesheet,
        "styleSheet": _stylesheet_getter,
        "setMinimumWidth": _noop,
        "setMinimumHeight": _min_h_setter,
        "minimumHeight": _min_h_getter,
        "setMinimumSize": _set_minimum_size,
        "minimumSize": _minimum_size,
        "setSizePolicy": _noop,
        "setFixedSize": _noop,
        "setFixedWidth": _noop,
        "setMaximumWidth": _set_maximum_width,
        "maximumWidth": _maximum_width_getter,
        "setHorizontalScrollBarPolicy": _set_horizontal_scrollbar_policy,
        "horizontalScrollBarPolicy": _horizontal_scrollbar_policy_getter,
        "setWordWrap": _set_word_wrap,
        "wordWrap": _word_wrap_getter,
        "setSingleStep": _noop,
        "setValue": _noop,
        "setEnabled": _set_enabled,
        "isEnabled": _is_enabled,
        "setToolTip": _set_tooltip,
        "setFocus": _set_focus,
        "hasFocus": _has_focus,
        "setFocusPolicy": _set_focus_policy,
        "focusPolicy": _focus_policy,
        "toolTip": _tooltip_getter,
        "setAccessibleName": _set_accessible_name,
        "accessibleName": _accessible_name_getter,
        "setTextFormat": _set_text_format,
        "textFormat": _text_format_getter,
        "setTextInteractionFlags": _noop,
        "setOpenExternalLinks": _noop,
        "setContentsMargins": _noop,
        "setSpacing": _noop,
        "setFixedHeight": _fixed_h_setter,
        "height": _fixed_h_getter,
        "setMaximumHeight": _max_h_setter,
        "maximumHeight": _max_h_getter,
        "setCheckable": _noop,
        "setChecked": _checked_setter,
        "isChecked": _checked_getter,
        "setText": _text_setter,
        "setAlignment": _noop,
        "setStatusTip": _noop,
        "setWhatsThis": _noop,
        "addLayout": _layout_add_layout,
        "addWidget": _layout_add_widget,
        "addStretch": _layout_add_stretch,
        "addItem": _layout_add_widget,
        "itemAt": _layout_item_at,
        "layout": _layout_getter,
        "showEvent": _noop,
        "hideEvent": _noop,

        "setToolButtonStyle": _noop,
        "setArrowType": _noop,
        "setPopupMode": _noop,
        "setDefault": _noop,
        "setDisabled": _noop,
        "setHidden": _noop,
        "setIcon": _noop,
        "setPlaceholderText": _set_placeholder_text,
        "placeholderText": _placeholder_text_getter,
        "setEchoMode": _noop,
        "setReadOnly": _noop,
        "fontMetrics": _font_metrics,
        "setRange": _noop,
        "setPrefix": _noop,
        "setSuffix": _noop,
        "setDecimals": _noop,
        "setValidator": _noop,
        "setHorizontalSpacing": _noop,
        "setFieldGrowthPolicy": _noop,
        "setRowWrapPolicy": _noop,
        "setLabelAlignment": _noop,
        "setFormAlignment": _noop,
        "setRowCount": _noop,
        "setCellWidget": _noop,
        "setItem": _noop,
        "setHeaderItem": _noop,
        "setHeaderLabels": _noop,
        "setHeaderHidden": _noop,
        "setRootIsDecorated": _noop,
        "setIndentation": _noop,
        "setExpandsOnDoubleClick": _noop,
        "setSelectionMode": _noop,
        "setAlternatingRowColors": _noop,
        "setUniformRowHeights": _noop,
        "setSectionResizeMode": _noop,
        "setTabOrder": _noop,
        "setCurrentCell": _noop,
        "setCurrentItem": _noop,
        "setCurrentRow": _noop,
        "setCurrentText": _noop,
        "setCurrentPage": _noop,
        "setMinimum": _noop,
        "setMaximum": _noop,
        "setOrientation": _noop,
        "setInvertedControls": _noop,
        "setPageStep": _noop,
        "setTickPosition": _noop,
        "setCentralWidget": _noop,
        "setStatusBar": _noop,
        "setWindowFlag": _noop,
        "setWindowFlags": _noop,
        "setWindowOpacity": _noop,
        "setWindowState": _noop,
        "setAnimated": _noop,
        "setDirection": _noop,
        "setFrameShadow": _noop,
        "setLineWidth": _noop,
        "setMidLineWidth": _noop,
        "setWidgetResizable": _noop,
        "setWidget": _noop,
        "setTitle": _noop,
        "setFlat": _noop,
        "setIconSize": _noop,
        "setCursor": _noop,
        "setAttribute": _noop,
        "setContextMenuPolicy": _noop,
        "setAcceptDrops": _noop,
        "setDragDropMode": _noop,
        "setDragEnabled": _noop,
        "setAcceptRichText": _noop,
        "setLineWrapMode": _noop,
        "setLineWrapColumnOrWidth": _noop,
        "setWordWrapMode": _noop,
        "setUndoRedoEnabled": _noop,
        "setCenterOnScroll": _noop,
        "setResizeMode": _noop,
        "setIsCurrentItem": _noop,
        "setSelected": _noop,
        "setTextElideMode": _noop,
        "setResizeAnchor": _noop,
        "setRenderHint": _noop,
        "setViewport": _noop,
        "setTransformationAnchor": _noop,
        "setDragMode": _noop,
        "setCacheMode": _noop,
        "setOptimizationFlags": _noop,
        "setMouseTracking": _noop,
        "setTabPosition": _noop,
        "setTabsClosable": _noop,
        "setDocumentMode": _noop,
        "setUsesScrollButtons": _noop,
        "setDocument": _noop,
        "setUndoStack": _noop,
        "setShortcut": _noop,
        "setShortcutEnabled": _noop,
        "setAutoRepeat": _noop,
        "setAutoExclusive": _noop,
        "setAutoFillBackground": _noop,
        "setGraphicsEffect": _noop,
        "setItemDelegate": _noop,
        "setItemDelegateForColumn": _noop,
        "setItemDelegateForRow": _noop,
        "setModel": _noop,
        "setSourceModel": _noop,
        "setFilterFixedString": _noop,
        "setFilterRegExp": _noop,
        "setSortFilterProxyModel": _noop,
        "setCompleter": _noop,
        "setSortingEnabled": _noop,
        "setSelectionModel": _noop,
        "setItemSelected": _noop,
        "setCurrentScene": _noop,
        "setSceneRect": _noop,
        "setBackgroundBrush": _noop,
        "setForegroundBrush": _noop,
        "setItemIndexMethod": _noop,
        "setLayoutMode": _noop,
        "setSizeAdjustPolicy": _noop,
        # QTableWidget / QListWidget item-model accessors.
        # These return None so callers that try to introspect a
        # cell get a falsy value; the widget code is expected to
        # guard with ``is not None``. We intentionally don't return
        # a stub item here because most callers just call .text()
        # or .setText() on the result, which would still fail.
        # Test for the absence of an item via ``rowCount() == 0``
        # is more reliable than ``item(...) is None``.
        "setHorizontalHeaderLabels": _noop,
        "setColumnCount": _noop,
        "insertRow": _noop,
        "currentRow": lambda self: 0,
        "rowCount": lambda self: 0,
        "horizontalHeaderItem": lambda self, *a: None,
        "item": lambda self, *a: None,
        "clear": _noop,
        "itemData": lambda self, *a: None,
        "setSelectionBehavior": _noop,
        "setEditTriggers": _noop,
        "currentIndex": lambda self: 0,
        "moveToThread": _noop,
        "verticalHeader": lambda self: _HeaderStub(),
        "horizontalHeader": lambda self: _HeaderStub(),
        "setHtml": _noop,
        "setPlainText": _plain_text_setter,
        "toPlainText": _plain_text_getter,
        "setMarkdown": _noop,
        "setProperty": _noop,
        "setData": _noop,
        "setFlags": _noop,
        "setState": _noop,
        "setCheckState": _noop,
        "setTristate": _noop,
        "setNoChange": _noop,
        "setStyle": _noop,
        "setLocale": _noop,
        "setInputMethodHints": _noop,
        "setGraphicsItem": _noop,
        "clicked": _PerInstanceSignal(),
        "deleteLater": _noop,
        "triggered": _PerInstanceSignal(),
        "toggled": _PerInstanceSignal(),
        "pressed": _PerInstanceSignal(),
        "released": _PerInstanceSignal(),
        "currentChanged": _PerInstanceSignal(),
        "currentIndexChanged": _PerInstanceSignal(),
        "currentTextChanged": _PerInstanceSignal(),
        "stateChanged": _PerInstanceSignal(),
        "valueChanged": _PerInstanceSignal(),
        "textChanged": _PerInstanceSignal(),
        "textEdited": _PerInstanceSignal(),
        "editingFinished": _PerInstanceSignal(),
        "returnPressed": _PerInstanceSignal(),
        "setVisible": _visible_setter,
        "isHidden": _hidden_getter,
        "setParent": _noop,
        "setLayout": _layout_setter,
        "resize": _noop,
        "resizeEvent": _noop,
        "sizeHint": lambda self: None,
        # QWidget window-related (used by QDialog subclasses)
        "setWindowTitle": _noop,
        "setWindowModality": _noop,
        "setSizeGripEnabled": _noop,
        # Geometry helpers for _HeightCachedLabel
        "width": lambda self: 0,
        "heightForWidth": lambda self, w: 0,
        # Visibility with tracking
        "hide": lambda self: setattr(self, "_visible", False),
        "show": lambda self: setattr(self, "_visible", True),
        "isVisible": _visible_getter,
        "update": _noop,
        "repaint": _noop,
        "close": lambda self: True,
        # Text with tracking
        "text": _text_getter,
        # Layout helpers (used by QVBoxLayout / QHBoxLayout / QFormLayout)
        "addRow": _noop,
        "insertWidget": _noop,
        "insertLayout": _noop,
    }
    return type(name, (), attrs)


def _make_qtimer_stub() -> type:
    """Build a QTimer stub that mimics the real QTimer's contract.

    The real QTimer is parented to a QObject, has setSingleShot / start
    / stop, and exposes a `timeout` signal that fires when the timer
    expires. The stub fires `timeout` synchronously inside start() —
    this matches the "0ms debounce" model that lets tests inspect
    signal payloads without spinning an event loop.
    """

    class _QTimer:
        def __init__(self, parent=None):
            self._parent = parent
            self._single_shot = False
            self._active = False
            self._interval = 0
            self.timeout = _Signal()

        def setSingleShot(self, single_shot: bool) -> None:
            self._single_shot = bool(single_shot)

        def setInterval(self, ms: int) -> None:
            self._interval = int(ms)

        def interval(self) -> int:
            return self._interval

        def start(self, ms: int = 0) -> None:
            self._active = True
            if ms:
                self._interval = int(ms)
            if self._single_shot:
                # Fire immediately so stubs behave like a 0ms debounce.
                self._active = False
                self.timeout.emit()

        def stop(self) -> None:
            self._active = False

        def deleteLater(self) -> None:
            """Match the real PySide6 ``QObject.deleteLater`` API.

            The stub does not schedule deferred deletion — we only need
            the method to exist so production code like
            ``_stop_history_poll_timer`` can call it when running under
            tests.
            """
            self._active = False

        def isActive(self) -> bool:
            return self._active

        @staticmethod
        def singleShot(ms: int, slot) -> None:
            """Static class-method used by ``rikugan.ui.theme.watcher``.

            Real PySide6 schedules a one-shot timer. The stub is a no-op
            so tests can patch this attribute to verify call counts
            without spinning an event loop.
            """
            return None

    return _QTimer


def _make_qcoreapplication_stub() -> type:
    """Build a minimal QCoreApplication stub for tests.

    The full PySide6 classmethod contract (instance(), quit(),
    sendPostedEvents(), etc.) is not needed by Rikugan tests. Only
    ``processEvents()`` is exercised (and only as a no-op flush). The
    QTimer stub already fires synchronously on start(), so
    ``processEvents`` does not need to dispatch anything for the
    ThemeManager debounce path.
    """

    class _QCoreApplication:
        @staticmethod
        def processEvents() -> None:
            return None

    return _QCoreApplication


def _make_qthread_stub() -> type:
    """Build a minimal QThread stub for tests.

    The real QThread runs ``run()`` in a background thread. For
    Rikugan tests we only need to drive ``run()`` synchronously so
    signal emissions land in the test's own list. ``start()`` just
    calls ``run()`` directly; ``quit()`` / ``wait()`` are no-ops.
    Class-level ``Signal`` attributes work because the descriptor
    protocol on ``_Signal`` does not care about the host class.
    """

    class _QThread:
        def __init__(self, *a, **kw):
            self._started = False

        def start(self) -> None:
            self._started = True
            self.run()

        def quit(self) -> None:
            return None

        def wait(self, *_a, **_kw) -> None:
            return None

        def isRunning(self) -> bool:
            return self._started

    return _QThread


class _Signal:
    """Minimal Signal stub that acts as a descriptor.

    Tracks connected slots in a list and invokes them on emit, so tests
    can verify signal-driven behavior (e.g. ``sig.connect(lambda x: ...)``
    followed by ``sig.emit(value)``). Disconnecting a slot not in the
    list is a no-op, matching PySide6 semantics.

    When accessed as a class-level attribute (e.g. ``QPushButton.clicked``)
    a fresh ``_Signal`` is materialised on the instance the first time
    the attribute is read. This mirrors how real PySide6 binds a Signal
    descriptor to the instance on attribute access — class-level
    descriptors must NOT share connection state across widget instances.
    """

    def __init__(self, *a):
        self._connections: list = []

    def connect(self, slot):
        self._connections.append(slot)

    def disconnect(self, *slots):
        if not slots:
            self._connections.clear()
            return
        for slot in slots:
            if slot in self._connections:
                self._connections.remove(slot)

    def emit(self, *a):
        for slot in self._connections:
            slot(*a)

    def __set_name__(self, owner, name: str) -> None:
        self._attr_name = f"_signal_{name}"

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        # Real PySide6 binds the descriptor on attribute access. We do
        # the same here so two ``HistoryPanel`` instances each get a
        # fresh connections list.
        attr = getattr(self, "_attr_name", None)
        if attr is None:
            # Class-level access without __set_name__ (e.g. when a test
            # instantiates ``Signal(str)`` directly) — fall back to a
            # shared signal so the bare class-level use case still
            # works.
            return self
        signal = getattr(obj, attr, None)
        if signal is None:
            signal = _Signal()
            try:
                setattr(obj, attr, signal)
            except Exception:
                # Some objects refuse attribute assignment; return the
                # shared signal so the call still succeeds.
                return self
        return signal

    def __set__(self, obj, value: object) -> None:
        # Allow explicit assignment (e.g. widget subclasses that set
        # ``self.session_open_requested = ...`` in __init__) to
        # override the descriptor's instance binding.
        attr = getattr(self, "_attr_name", None)
        if attr is not None:
            try:
                setattr(obj, attr, value)
            except Exception:
                pass


class _PerInstanceSignal(_Signal):
    """Backward-compatible alias for the descriptor (older call sites)."""

    def __init__(self) -> None:
        super().__init__()


_WIDGET_NAMES = [
    "QAbstractItemView",
    "QApplication",
    "QCheckBox",
    "QComboBox",
    "QDialog",
    "QDialogButtonBox",
    "QDoubleSpinBox",
    "QFileDialog",
    "QFormLayout",
    "QFrame",
    "QGroupBox",
    "QHBoxLayout",
    "QHeaderView",
    "QLabel",
    "QLineEdit",
    "QListWidget",
    "QListWidgetItem",
    "QMenu",
    "QMessageBox",
    "QPlainTextEdit",
    "QProgressBar",
    "QPushButton",
    "QRadioButton",
    "QScrollArea",
    "QSizePolicy",
    "QSpinBox",
    "QSplitter",
    "QStackedWidget",
    "QTabBar",
    "QTableWidget",
    "QTableWidgetItem",
    "QTabWidget",
    "QTextEdit",
    "QToolButton",
    "QTreeWidget",
    "QTreeWidgetItem",
    "QVBoxLayout",
    "QWidget",
]

_GUI_NAMES = [
    "QColor",
    "QFont",
    "QIntValidator",
    "QKeySequence",
    "QPalette",
    "QShortcut",
    "QSyntaxHighlighter",
    "QTextCharFormat",
]


def _stub_mod(name: str, **attrs) -> types.ModuleType:
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    return m


def ensure_pyside6_stubs() -> None:
    """Install minimal PySide6 stubs into sys.modules (idempotent)."""
    global _installed
    if _installed:
        return
    _installed = True

    _sentinel = type("_Qt", (), {})()
    _sentinel.ItemDataRole = type("_ItemDataRole", (), {"UserRole": 32})()
    _sentinel.TextFormat = type("_TextFormat", (), {"PlainText": 0, "RichText": 1, "AutoText": 2})()
    # Production code (rikugan/ui/message_widgets.py, tool_widgets.py) uses
    # the ``Qt.TextInteractionFlag(A.value | B.value)`` pattern to bypass
    # IDA 9.4's PyQt5-shim ``__or__`` interceptor. That pattern needs the
    # enum-like ``.value`` attribute and a callable wrapper, so the stub
    # mirrors PySide6's IntFlag for this one enum. Other Qt enums keep the
    # plain-int stub (production code still uses ``|`` directly on them).
    _sentinel.TextInteractionFlag = IntFlag(
        "_TextInteractionFlag",
        {
            "NoTextInteraction": 0,
            "TextSelectableByMouse": 1,
            "TextSelectableByKeyboard": 2,
            "TextEditable": 4,
            "TextEditorInteraction": 6,
            "TextBrowserInteraction": 13,
            "LinksAccessibleByMouse": 8,
            "LinksAccessibleByKeyboard": 16,
        },
    )
    _sentinel.AlignmentFlag = type(
        "_AlignmentFlag",
        (),
        {
            "AlignLeft": 1,
            "AlignRight": 2,
            "AlignHCenter": 4,
            "AlignTop": 32,
            "AlignBottom": 64,
            "AlignVCenter": 128,
            "AlignCenter": 132,
            "AlignAbsolute": 16,
            "AlignLeading": 1,
            "AlignTrailing": 2,
        },
    )()
    _sentinel.Orientation = type("_Orientation", (), {"Horizontal": 1, "Vertical": 2})()
    # Qt.FocusPolicy enum — used by QPushButton.setFocusPolicy so a
    # keyboard user can reach the row's delete button via Tab. The
    # stub only needs the values production code references
    # (``StrongFocus``); other policy values are trivially 0.
    _sentinel.FocusPolicy = type(
        "_FocusPolicy",
        (),
        {
            "NoFocus": 0,
            "TabFocus": 1,
            "ClickFocus": 2,
            "StrongFocus": 11,
            "WheelFocus": 15,
        },
    )()
    # Mirror real Qt's QWidget default (``NoFocus``) so the stub returns
    # the documented default when ``setFocusPolicy`` was never called.
    # Production code that wants the widget in the tab chain must call
    # ``setFocusPolicy(StrongFocus)`` explicitly; tests then pin that
    # explicit pin via ``assertEqual(policy, Qt.FocusPolicy.StrongFocus)``.
    global _DEFAULT_FOCUS_POLICY
    _DEFAULT_FOCUS_POLICY = _sentinel.FocusPolicy.NoFocus
    _sentinel.WindowModality = type(
        "_WindowModality",
        (),
        {"NonModal": 0, "WindowModal": 1, "ApplicationModal": 2},
    )()
    _sentinel.StandardButton = type(
        "_StandardButton",
        (),
        {
            "NoButton": 0,
            "Ok": 1,
            "Cancel": 2,
            "Yes": 3,
            "No": 4,
            "Save": 32,
            "Open": 1024,
            "Close": 2048,
            "Apply": 33554432,
        },
    )()
    # Qt.ScrollBarPolicy enum — production code (HistoryPanel, etc.)
    # uses ScrollBarAlwaysOff to disable horizontal scrolling on a
    # vertical-only list. Real PySide6 exposes the same numeric
    # values; tests assert the constant so the API call is verified
    # end-to-end.
    _sentinel.ScrollBarPolicy = type(
        "_ScrollBarPolicy",
        (),
        {
            "ScrollBarAsNeeded": 0,
            "ScrollBarAlwaysOff": 1,
            "ScrollBarAlwaysOn": 2,
        },
    )()
    # Qt.ShortcutContext enum — production code (panel_core QShortcuts)
    # scopes shortcuts to the panel window so they never leak into the
    # host's global shortcut namespace. Values mirror real Qt.
    _sentinel.ShortcutContext = type(
        "_ShortcutContext",
        (),
        {
            "WidgetShortcut": 0,
            "WidgetWithChildrenShortcut": 3,
            "WindowShortcut": 1,
            "ApplicationShortcut": 2,
        },
    )()

    sys.modules.setdefault("PySide6", _stub_mod("PySide6"))
    sys.modules.setdefault(
        "PySide6.QtCore",
        _stub_mod(
            "PySide6.QtCore",
            Signal=_Signal,
            QEvent=_qt_class("QEvent"),
            Qt=_sentinel,
            QObject=_qt_class("QObject"),
            QTimer=_make_qtimer_stub(),
            # Minimal QCoreApplication stub — real tests that need a
            # real event loop should drop these stubs and re-import
            # PySide6 (see test_theme_watcher.py / test_theme_manager.py).
            # ``processEvents`` is a no-op here; the QTimer stub fires
            # synchronously on start() so the debounce in ThemeManager
            # does not need a real event loop to dispatch.
            QCoreApplication=_make_qcoreapplication_stub(),
            QThread=_make_qthread_stub(),
        ),
    )

    # QSizePolicy needs nested Policy enum
    _size_policy = _qt_class("QSizePolicy")
    _size_policy.Policy = type(
        "_SizePolicyPolicy",
        (),
        {
            "Fixed": 0,
            "Minimum": 1,
            "Maximum": 4,
            "Preferred": 5,
            "Expanding": 7,
            "MinimumExpanding": 3,
            "Ignored": 13,
        },
    )()

    # QFont needs a nested Weight enum (QFont.Weight.Bold) so the syntax
    # highlighter can mark keywords as bold without hitting AttributeError.
    _qfont = _qt_class("QFont")
    _qfont.Weight = type(
        "_QFontWeight",
        (),
        {
            "Thin": 0,
            "ExtraLight": 12,
            "Light": 25,
            "Normal": 50,
            "Medium": 63,
            "DemiBold": 75,
            "Bold": 75,
            "ExtraBold": 81,
            "Black": 87,
        },
    )()

    _widget_stubs = {n: _qt_class(n) for n in _WIDGET_NAMES}
    _widget_stubs["QSizePolicy"] = _size_policy

    # QLayout subclasses (QVBoxLayout / QHBoxLayout / QFormLayout) call
    # ``QLayout(parent)`` to wire the layout to a widget. production code
    # then calls ``widget.layout()`` to look it back up. The standard
    # ``__init__`` is a no-op, so we replace it for these four classes to
    # auto-register with the parent.
    def _layout_init(self, parent=None, *a, **k):
        # Initialise items list once for this instance.
        self._items = []
        if parent is not None:
            parent.setLayout(self)

    for _layout_name in (
        "QVBoxLayout",
        "QHBoxLayout",
        "QFormLayout",
        "QGridLayout",
        "QStackedLayout",
    ):
        if _layout_name in _widget_stubs:
            _widget_stubs[_layout_name].__init__ = _layout_init

    # QLineEdit needs a nested EchoMode enum (used by setEchoMode).
    _widget_stubs["QLineEdit"].EchoMode = type(
        "_LineEditEchoMode",
        (),
        {"Normal": 0, "NoEcho": 1, "Password": 2, "PasswordEchoOnEdit": 3},
    )()
    # QAbstractItemView SelectionMode / EditTrigger (used by QListWidget,
    # QTableWidget, QTreeWidget).
    _widget_stubs["QAbstractItemView"].SelectionMode = type(
        "_SelMode",
        (),
        {"SingleSelection": 1, "MultiSelection": 2, "ExtendedSelection": 3, "ContiguousSelection": 4, "NoSelection": 0},
    )()
    _widget_stubs["QAbstractItemView"].SelectionBehavior = type(
        "_SelBeh",
        (),
        {"SelectItems": 0, "SelectRows": 1, "SelectColumns": 2},
    )()
    _widget_stubs["QAbstractItemView"].EditTrigger = type(
        "_EditTrig",
        (),
        {
            "NoEditTriggers": 0,
            "CurrentChanged": 1,
            "DoubleClicked": 2,
            "SelectedClicked": 4,
            "EditKeyPressed": 8,
            "AnyKeyPressed": 16,
            "AllEditTriggers": 31,
        },
    )()
    _widget_stubs["QAbstractItemView"].DragDropMode = type(
        "_DDMode",
        (),
        {"NoDragDrop": 0, "DragOnly": 1, "DropOnly": 2, "DragDrop": 3, "InternalMove": 4},
    )()
    _widget_stubs["QDialog"].DialogCode = type(
        "_DialogCode",
        (),
        {"Accepted": 1, "Rejected": 0},
    )()
    # QDialog subclasses (SettingsDialog) call self.accept() / self.reject()
    # from the Ok/Cancel button wiring.
    _widget_stubs["QDialog"].accept = lambda self: None
    _widget_stubs["QDialog"].reject = lambda self: None
    _widget_stubs["QDialog"].done = lambda self, r: None
    _widget_stubs["QDialog"].exec = lambda self: 0
    _widget_stubs["QDialog"].open = lambda self: None
    # QPlainTextEdit.LineWrapMode is referenced by tool_widgets to disable
    # word wrap on the code editor (``QPlainTextEdit.LineWrapMode.NoWrap``).
    _widget_stubs["QPlainTextEdit"].LineWrapMode = type(
        "_LineWrapMode",
        (),
        {"NoWrap": 0, "WidgetWidth": 1, "FixedPixelWidth": 2},
    )()
    # tool_widgets passes ``self._code_edit.document()`` to the Python
    # syntax highlighter; the stub just returns a sentinel object — the
    # highlighter doesn't introspect it during construction.
    _widget_stubs["QPlainTextEdit"].document = lambda self: object()
    _widget_stubs["QDialogButtonBox"].StandardButton = type(
        "_DialogBoxStandardButton",
        (),
        {
            "NoButton": 0,
            "Ok": 1024,
            "Open": 8192,
            "Save": 2048,
            "Cancel": 4194304,
            "Close": 2097152,
            "Discard": 8388608,
            "Apply": 33554432,
            "Reset": 67108864,
            "RestoreDefaults": 134217728,
            "Help": 16777216,
            "SaveAll": 268435456,
            "Yes": 16384,
            "YesToAll": 32768,
            "No": 65536,
            "NoToAll": 131072,
            "Abort": 262144,
            "Retry": 524288,
            "Ignore": 1048576,
        },
    )()
    # QDialogButtonBox needs accepted/rejected/clicked signals for the
    # Ok/Cancel wiring in SettingsDialog.
    _widget_stubs["QDialogButtonBox"].accepted = _Signal()
    _widget_stubs["QDialogButtonBox"].rejected = _Signal()
    _widget_stubs["QDialogButtonBox"].clicked = _Signal()
    _widget_stubs["QFrame"].Shape = type(
        "_FrameShape",
        (),
        {"NoFrame": 0, "Box": 1, "Panel": 2, "StyledPanel": 6, "HLine": 4, "VLine": 5, "WinPanel": 3},
    )()
    _widget_stubs["QFrame"].Shadow = type(
        "_FrameShadow",
        (),
        {"Plain": 16, "Raised": 32, "Sunken": 48},
    )()

    # QFont lives under QtGui, but we build it here so we can attach the
    # nested Weight enum before exposing it to the module below.
    sys.modules.setdefault(
        "PySide6.QtGui",
        _stub_mod(
            "PySide6.QtGui",
            **{n: _qt_class(n) for n in _GUI_NAMES},
        ),
    )
    sys.modules["PySide6.QtGui"].QFont = _qfont

    # QAccessibleAnnouncementEvent is the canonical PySide6 class for
    # notifying screen readers about a transient message. The stub
    # mirrors the real constructor signature ``(object, message)`` and
    # exposes the payload through ``message()`` so tests can assert
    # both the event type and the announced text without a real
    # accessibility bridge.
    class _AccessibleAnnouncementEvent:
        """Stub for ``PySide6.QtGui.QAccessibleAnnouncementEvent``.

        Real PySide6 constructs an announcement event with
        ``QAccessibleAnnouncementEvent(obj, message)`` and posts it to
        ``QAccessible.updateAccessibility``. The stub preserves the
        constructor signature and exposes the message via ``message()``
        so production code reads identically with either the real
        binding or this stub.
        """

        def __init__(self, obj: object = None, message: str = "") -> None:
            self._object = obj
            self._message = str(message)
            self._politeness = "Polite"

        def message(self) -> str:
            return self._message

        def setPoliteness(self, politeness: object) -> None:
            self._politeness = politeness

        def politeness(self) -> object:
            return self._politeness

        def accessibleObject(self) -> object:
            return self._object

    sys.modules["PySide6.QtGui"].QAccessibleAnnouncementEvent = _AccessibleAnnouncementEvent

    # QAccessible namespace — only the entries production code reads.
    # ``updateAccessibility(event)`` is the documented delivery path
    # for accessibility notifications; the stub forwards the event
    # into the per-widget ``_announcements`` sink so tests can assert
    # what was announced without bridging to a real screen reader.
    class _AccessibleNamespace:
        @staticmethod
        def updateAccessibility(event: object) -> None:
            target = getattr(event, "accessibleObject", lambda: None)()
            if target is None:
                return
            sink = getattr(target, "_announcements", None)
            if sink is None:
                sink = []
                target._announcements = sink
            entry = (event, event.message() if hasattr(event, "message") else "")
            sink.append(entry)
            # Forward to the test-installed recorder if any.
            test_sink = getattr(target, "_announcements_sink", None)
            if test_sink is not None:
                test_sink(entry)

    sys.modules["PySide6.QtGui"].QAccessible = _AccessibleNamespace

    sys.modules.setdefault(
        "PySide6.QtWidgets",
        _stub_mod("PySide6.QtWidgets", **_widget_stubs),
    )

    # QApplication needs a small state-tracking stub so that
    # QApplication.primaryScreen() and screen.availableGeometry() return
    # a usable geometry in tests. The real QApplication is a singleton
    # managed by Qt; the stub keeps the same staticmethod contract so
    # code like ``QApplication.primaryScreen()`` works without an event
    # loop. ``processEvents`` and ``setStyleSheet`` are no-ops so the
    # ThemeManager's QSS-rebuild path is silent in tests.
    def _qapp_primary_screen():
        return _qapp_screen_stub

    def _qapp_screen_geometry():
        class _Geom:
            def __init__(self, w: int, h: int) -> None:
                self._w = w
                self._h = h

            def width(self) -> int:
                return self._w

            def height(self) -> int:
                return self._h

        return _Geom(1920, 1080)

    class _QAppScreen:
        def availableGeometry(self):
            return _qapp_screen_geometry()

    _qapp_screen_stub = _QAppScreen()

    def _make_qapplication_stub() -> type:
        class _QApplication:
            @staticmethod
            def instance():
                return _qapp_singleton

            @staticmethod
            def primaryScreen():
                return _qapp_screen_stub

            def __init__(self, *a, **k):
                pass

            def setStyleSheet(self, qss: str) -> None:
                # Track so tests can assert that _apply_now does not
                # clobber the host's stylesheet.
                _qapp_singleton._stylesheet = qss

            def styleSheet(self) -> str:
                return getattr(_qapp_singleton, "_stylesheet", "")

            def processEvents(self) -> None:
                return None

        _qapp_singleton = _QApplication()
        return _QApplication

    sys.modules["PySide6.QtWidgets"].QApplication = _make_qapplication_stub()

    # Replace QColor and QTextCharFormat with state-tracking stubs so the
    # syntax highlighter test can verify that palette colours flow through
    # to the QTextCharFormat foreground property. Plain `_qt_class` stubs
    # would be no-ops and swallow the colour information.
    def _qcolor_init(self, name=""):
        self._name = str(name).lower()

    def _qcolor_name(self):
        return self._name

    def _qtext_char_format_init(self):
        self._fg = _qcolor("")
        self._bold = False
        self._italic = False

    def _qtext_char_format_set_foreground(self, c):
        self._fg = c

    def _qtext_char_format_foreground(self):
        return self._fg

    def _qtext_char_format_set_font_weight(self, w):
        self._bold = w

    def _qtext_char_format_set_font_italic(self, i):
        self._italic = bool(i)

    def _qtext_char_format_font_italic(self):
        return self._italic

    def _qtext_char_format_font_weight(self):
        return self._bold

    _qcolor = type(
        "QColor",
        (),
        {
            "__init__": _qcolor_init,
            "name": _qcolor_name,
        },
    )
    _qtext_char_format = type(
        "QTextCharFormat",
        (),
        {
            "__init__": _qtext_char_format_init,
            "setForeground": _qtext_char_format_set_foreground,
            "foreground": _qtext_char_format_foreground,
            "setFontWeight": _qtext_char_format_set_font_weight,
            "fontWeight": _qtext_char_format_font_weight,
            "setFontItalic": _qtext_char_format_set_font_italic,
            "fontItalic": _qtext_char_format_font_italic,
        },
    )
    sys.modules["PySide6.QtGui"].QColor = _qcolor
    sys.modules["PySide6.QtGui"].QTextCharFormat = _qtext_char_format

    # QPainter is a no-op stub for tests. paintEvent() methods can call
    # ``p = QPainter(self); ...; p.end()`` without crashing in the test
    # environment. The ThemePreviewChip's paintEvent exercises the full
    # QPainter surface (fillRect, setPen, drawText, end) — the stub
    # absorbs all of these as no-ops.
    def _qpainter_init(self, *a, **k):
        return None

    def _qpainter_end(self):
        return None

    def _qpainter_noop(self, *a, **k):
        return None

    _qpainter = type(
        "QPainter",
        (),
        {
            "__init__": _qpainter_init,
            "end": _qpainter_end,
            "fillRect": _qpainter_noop,
            "setPen": _qpainter_noop,
            "drawText": _qpainter_noop,
        },
    )
    sys.modules["PySide6.QtGui"].QPainter = _qpainter

    # QTabWidget and QComboBox need state-tracking stubs so the
    # Appearance tab tests can verify addTab/count/tabText/addItem/itemData
    # and that setCurrentIndex fires currentIndexChanged synchronously
    # (matching the real Qt behavior in single-threaded test mode).
    def _make_qtabwidget_stub() -> type:
        class _QTabWidget:
            def __init__(self, parent=None):
                self._tabs: list = []  # list of (label, widget, data)
                self._current = 0
                self.currentChanged = _Signal()
                self.currentIndexChanged = _Signal()

            def addTab(self, widget, label):
                idx = len(self._tabs)
                self._tabs.append((label, widget, None))
                return idx

            def insertTab(self, index, widget, label):
                # Clamp to [0, len] like real Qt.
                index = max(0, min(index, len(self._tabs)))
                self._tabs.insert(index, (label, widget, None))
                return index

            def count(self):
                return len(self._tabs)

            def tabText(self, idx):
                if 0 <= idx < len(self._tabs):
                    return self._tabs[idx][0]
                return ""

            def widget(self, idx):
                if 0 <= idx < len(self._tabs):
                    return self._tabs[idx][1]
                return None

            def currentIndex(self):
                return self._current

            def setCurrentIndex(self, idx):
                if idx == self._current:
                    return
                self._current = idx
                self.currentChanged.emit(idx)
                self.currentIndexChanged.emit(idx)

        return _QTabWidget

    def _make_qcombobox_stub() -> type:
        class _QComboBox:
            def __init__(self, parent=None):
                self._items: list = []  # list of (label, data)
                self._current = -1
                self._editable = False
                self._text = ""
                self._blocked = 0
                self.currentIndexChanged = _Signal()
                self.currentTextChanged = _Signal()
                self.activated = _Signal()
                self.highlighted = _Signal()

            def addItem(self, label, data=None):
                self._items.append((label, data))

            def insertItem(self, index, label, data=None):
                if index < 0:
                    index = 0
                if index > len(self._items):
                    index = len(self._items)
                self._items.insert(index, (label, data))

            def addItems(self, labels):
                for label in labels:
                    self.addItem(label)

            def count(self):
                return len(self._items)

            def itemData(self, idx):
                if 0 <= idx < len(self._items):
                    return self._items[idx][1]
                return None

            def itemText(self, idx):
                if 0 <= idx < len(self._items):
                    return self._items[idx][0]
                return ""

            def currentIndex(self):
                return self._current

            def setCurrentIndex(self, idx):
                if idx == self._current:
                    return
                if not (0 <= idx < len(self._items)):
                    return
                self._current = idx
                if not self._blocked:
                    self.currentIndexChanged.emit(idx)
                    self.currentTextChanged.emit(self._items[idx][0])

            def currentText(self):
                if 0 <= self._current < len(self._items):
                    return self._items[self._current][0]
                return ""

            def currentData(self, role=None):
                if 0 <= self._current < len(self._items):
                    return self._items[self._current][1]
                return None

            def setCurrentText(self, text):
                for i, (label, _data) in enumerate(self._items):
                    if label == text:
                        self.setCurrentIndex(i)
                        return

            def findText(self, text):
                for i, (label, _data) in enumerate(self._items):
                    if label == text:
                        return i
                return -1

            def findData(self, data):
                for i, (_label, d) in enumerate(self._items):
                    if d == data:
                        return i
                return -1

            def setEditable(self, editable):
                self._editable = bool(editable)

            def setMinimumWidth(self, w):
                return None

            def setFixedWidth(self, w):
                return None

            def setMaximumWidth(self, w):
                return None

            def setToolTip(self, *args, **kwargs):
                return None

            def setStatusTip(self, *args, **kwargs):
                return None

            def setSizeAdjustPolicy(self, p):
                return None

            def insertSeparator(self, index):
                # Stub no-op — real Qt inserts a visual separator item.
                return None

            def view(self):
                return None

            def model(self):
                return None

            def blockSignals(self, block):
                if block:
                    self._blocked += 1
                else:
                    self._blocked = max(0, self._blocked - 1)
                return True

            def clear(self):
                self._items.clear()
                self._current = -1

            def setEnabled(self, enabled):
                self._enabled = bool(enabled)

            def isEnabled(self):
                return getattr(self, "_enabled", True)

        return _QComboBox

    sys.modules["PySide6.QtWidgets"].QTabWidget = _make_qtabwidget_stub()
    sys.modules["PySide6.QtWidgets"].QComboBox = _make_qcombobox_stub()

    def _make_qspinbox_stub() -> type:
        """Build a minimal QSpinBox stub with value/range tracking.

        The generic widget stub does not track ``value()`` / ``maximum()``,
        but the GLM settings controls and the Max Output Tokens spin box
        depend on those for clamping and round-trip.
        """
        _Base = _qt_class("QSpinBoxBase")

        class _QSpinBox(_Base):
            def __init__(self, parent=None):
                super().__init__()
                self._value = 0
                self._minimum = 0
                self._maximum = 99
                self._suffix = ""
                self._single_step = 1

            def value(self):
                return self._value

            def setValue(self, v):
                v = int(v)
                if v > self._maximum:
                    v = self._maximum
                if v < self._minimum:
                    v = self._minimum
                self._value = v

            def minimum(self):
                return self._minimum

            def setMinimum(self, m):
                self._minimum = int(m)

            def maximum(self):
                return self._maximum

            def setMaximum(self, m):
                self._maximum = int(m)
                if self._value > self._maximum:
                    self._value = self._maximum

            def setRange(self, lo, hi):
                self._minimum = int(lo)
                self._maximum = int(hi)
                if self._value > self._maximum:
                    self._value = self._maximum
                if self._value < self._minimum:
                    self._value = self._minimum

            def setSingleStep(self, s):
                self._single_step = int(s)

            def setSuffix(self, s):
                self._suffix = str(s)

        return _QSpinBox

    sys.modules["PySide6.QtWidgets"].QSpinBox = _make_qspinbox_stub()

    def _make_qdoublespinbox_stub() -> type:
        """Build a QDoubleSpinBox stub with value/range/decimals tracking."""
        _Base = _qt_class("QDoubleSpinBoxBase")

        class _QDoubleSpinBox(_Base):
            def __init__(self, parent=None):
                super().__init__()
                self._value = 0.0
                self._minimum = 0.0
                self._maximum = 99.0
                self._single_step = 1.0
                self._decimals = 2

            def value(self):
                return self._value

            def setValue(self, v):
                v = float(v)
                if v > self._maximum:
                    v = self._maximum
                if v < self._minimum:
                    v = self._minimum
                self._value = v

            def minimum(self):
                return self._minimum

            def setMinimum(self, m):
                self._minimum = float(m)

            def maximum(self):
                return self._maximum

            def setMaximum(self, m):
                self._maximum = float(m)

            def setRange(self, lo, hi):
                self._minimum = float(lo)
                self._maximum = float(hi)

            def setSingleStep(self, s):
                self._single_step = float(s)

            def setDecimals(self, d):
                self._decimals = int(d)

        return _QDoubleSpinBox

    sys.modules["PySide6.QtWidgets"].QDoubleSpinBox = _make_qdoublespinbox_stub()

    def _make_qstackedwidget_stub() -> type:
        """Build a minimal QStackedWidget stub.

        Real PySide6 QStackedWidget switches between stacked children
        via ``setCurrentIndex`` / ``setCurrentWidget``. The widget has
        addWidget / removeWidget / count semantics; tests need the
        count + currentIndex surface so a panel-driven state machine
        can be exercised without a real event loop.

        We inherit from the regular ``_qt_class`` so the base QWidget
        attribute set (``setObjectName``, ``setStyleSheet``,
        ``setLayout``) is available alongside the stack semantics.
        """
        _Base = _qt_class("QStackedWidgetBase")

        class _QStackedWidget(_Base):
            def __init__(self, parent=None):
                super().__init__()
                self._pages: list = []
                self._current = -1
                self.currentChanged = _Signal()

            def addWidget(self, widget):
                self._pages.append(widget)
                idx = len(self._pages) - 1
                if self._current < 0:
                    self._current = idx
                return idx

            def removeWidget(self, widget):
                if widget in self._pages:
                    self._pages.remove(widget)
                    if self._current >= len(self._pages):
                        self._current = len(self._pages) - 1

            def count(self):
                return len(self._pages)

            def widget(self, idx):
                if 0 <= idx < len(self._pages):
                    return self._pages[idx]
                return None

            def currentIndex(self):
                return self._current

            def setCurrentIndex(self, idx):
                if 0 <= idx < len(self._pages) and idx != self._current:
                    self._current = idx
                    self.currentChanged.emit(idx)

            def currentWidget(self):
                return self.widget(self._current)

            def setCurrentWidget(self, widget):
                try:
                    idx = self._pages.index(widget)
                except ValueError:
                    return
                self.setCurrentIndex(idx)

        return _QStackedWidget

    sys.modules["PySide6.QtWidgets"].QStackedWidget = _make_qstackedwidget_stub()

    # QScrollArea needs a stateful vertical scrollbar so tests can
    # capture and restore the list position across ``remove_entry``
    # rerenders. The base ``_qt_class`` produces a no-op widget that
    # silently swallows ``verticalScrollBar()`` calls; the panel's
    # scroll-clamp logic relies on the stub returning a value-tracking
    # bar.
    class _ScrollBar:
        """Minimal QScrollBar stub for tests.

        The history panel captures the value before re-rendering rows
        and clamps it after the rerender to ``maximum()``. The stub
        tracks ``_value`` and ``_maximum`` as plain Python ints so the
        test can drive ``setValue`` and assert the clamp without an
        event loop.
        """

        def __init__(self) -> None:
            self._value = 0
            self._maximum = 0

        def value(self) -> int:
            return self._value

        def setValue(self, v: int) -> None:
            self._value = int(v)

        def maximum(self) -> int:
            return self._maximum

        def setMaximum(self, m: int) -> None:
            self._maximum = int(m)

    def _make_qscrollarea_stub() -> type:
        """Build a minimal QScrollArea stub with a stateful vertical bar.

        Each instance owns a private ``_ScrollBar`` so multiple panels
        in the same test process do not share scroll state. ``setWidget``
        is routed to a private attribute so re-parenting test fixtures
        don't fail when the widget loop is absent.
        """

        _Base = _qt_class("QScrollAreaBase")

        class _QScrollArea(_Base):
            def __init__(self, parent=None):
                super().__init__()
                self._vertical = _ScrollBar()

            def verticalScrollBar(self) -> _ScrollBar:
                return self._vertical

            def setWidget(self, widget) -> None:
                self._widget = widget

            def setWidgetResizable(self, _resizable: bool) -> None:
                return None

        return _QScrollArea

    sys.modules["PySide6.QtWidgets"].QScrollArea = _make_qscrollarea_stub()

    # QLayout subclasses (QVBoxLayout/QHBoxLayout) used by stack
    # containers need ``removeWidget`` so the history panel can drop a
    # row before the trailing stretch. The default ``_qt_class`` does
    # not track the ``layout.addWidget`` calls, so ``removeWidget``
    # would silently no-op; the real class returns ``True`` on
    # success.
    _qvb_stub = sys.modules["PySide6.QtWidgets"].QVBoxLayout
    _qhb_stub = sys.modules["PySide6.QtWidgets"].QHBoxLayout

    def _layout_remove_widget(self, w):
        items = getattr(self, "_items", [])
        if w in items:
            items.remove(w)
            return True
        return False

    if not hasattr(_qvb_stub, "removeWidget"):
        _qvb_stub.removeWidget = _layout_remove_widget
        _qhb_stub.removeWidget = _layout_remove_widget

    def _layout_count(self):
        return len(getattr(self, "_items", []))

    if not hasattr(_qvb_stub, "count"):
        _qvb_stub.count = _layout_count
        _qhb_stub.count = _layout_count
