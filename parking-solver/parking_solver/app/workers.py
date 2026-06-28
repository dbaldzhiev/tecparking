"""Qt worker thread for the background exploration.

Architecture rule: this module is the *only* place that bridges the core
(plain Python) and the Qt threading primitives.  The core never imports Qt;
Qt slots on the main thread receive plain dataclasses via signals.
"""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from parking_solver.core.generator import generate_all
from parking_solver.core.model import Site
from parking_solver.core.regulations.engine import RegulationProfile


class ExploreWorker(QThread):
    """Runs generate_all() in a background thread, streaming results live.

    Signals
    -------
    progress(int, int)         — (done, total) tasks
    result_ready(object)       — one StrategyResult, emitted as soon as it's built
    finished_ok(list)          — final list[StrategyResult] sorted by stall count
    failed(str)                — exception message
    """

    progress: Signal = Signal(int, int)
    result_ready: Signal = Signal(object)  # StrategyResult
    finished_ok: Signal = Signal(object)   # list[StrategyResult]
    failed: Signal = Signal(str)

    def __init__(
        self,
        site: Site,
        profile: RegulationProfile,
        stall_width: float = 2.5,
        stall_length: float = 5.0,
        parent=None,
    ):
        super().__init__(parent)
        self._site = site
        self._profile = profile
        self._stall_width = stall_width
        self._stall_length = stall_length

    def run(self) -> None:
        try:
            results = generate_all(
                self._site,
                self._profile,
                stall_width=self._stall_width,
                stall_length=self._stall_length,
                progress_callback=lambda d, t: self.progress.emit(d, t),
                result_callback=lambda r: self.result_ready.emit(r),
            )
            self.finished_ok.emit(results)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
