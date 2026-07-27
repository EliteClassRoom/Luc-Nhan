"""Qt binding surface for Rikugan.

Rikugan targets IDA Pro ≥ 9.0, which ships PySide6 (Qt6) as its sole Qt
binding. (IDA 9.x also exposes a legacy Qt5-named module, but it is a
thin shim that delegates to PySide6 — not a separate binding.) Rikugan
imports PySide6 directly and never relies on that shim.

This module is the primary import seam for Qt symbols across the
package. Most call sites import from ``rikugan.ui.qt_compat`` rather
than from ``PySide6`` directly, so a future host binding swap (e.g.
PySide7) should only require editing this one file. A few theme modules
(``rikugan/ui/theme/manager.py``, ``palette_ida.py``, ``watcher.py``)
import ``PySide6`` directly behind a ``try/except ImportError`` so the
headless/non-Qt path can skip them; consolidating those is future work.

Previously this module also supported an alternative binding via runtime
detection. That detection was the root cause of the IDA 9.1 crash: when
another plugin had pre-imported the alternative, detection picked it
while the host still ran PySide6, producing mismatched widget types in
widget constructors
(``QVBoxLayout(QWidget): argument 1 has unexpected type
'PySide6.QtWidgets.QWidget'``). The alternative binding has been removed
entirely.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt, QThread, QTimer, Signal  # noqa: F401
from PySide6.QtGui import (  # noqa: F401
    QAccessible,
    QAccessibleAnnouncementEvent,
    QColor,
    QFont,
    QIntValidator,
    QKeySequence,
    QPainter,
    QPalette,
    QShortcut,
    QSyntaxHighlighter,
    QTextCharFormat,
)
from PySide6.QtWidgets import (  # noqa: F401
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTabBar,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

# IDA 9.x's legacy PyQt5 shim intercepts Qt flag ``|`` and emits a
# RuntimeWarning (breaking if the shim is absent). OR-ing ``.value`` and
# rebuilding the flag sidesteps the shim. Only these two combos are used.
OK_CANCEL_BUTTONS = QDialogButtonBox.StandardButton(
    QDialogButtonBox.StandardButton.Ok.value | QDialogButtonBox.StandardButton.Cancel.value
)
YES_NO_BUTTONS = QMessageBox.StandardButton(QMessageBox.StandardButton.Yes.value | QMessageBox.StandardButton.No.value)
