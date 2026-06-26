"""Qt worker thread for the multi-objective optimizer.

Architecture rule: this module is the *only* place that bridges the core
(plain Python) and the Qt threading primitives.  The optimizer never imports
Qt; Qt slots on the main thread receive plain dataclasses via signals.
"""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from parking_solver.core.model import Site
from parking_solver.core.optimizer import (
    Candidate,
    OptimizationParams,
    ParetoResult,
    run,
)
from parking_solver.core.regulations.engine import RegulationProfile


class OptimizeWorker(QThread):
    """Runs the NSGA-II optimizer in a background thread.

    Signals
    -------
    generation_ready(int, list[Candidate])
        Emitted after each generation with the current Pareto front.
    finished_ok(ParetoResult)
        Emitted once when the run completes successfully.
    failed(str)
        Emitted if an exception is raised during the run.
    progress(int, int)
        Emitted as (current_gen, total_gen) for a progress bar.
    """

    generation_ready: Signal = Signal(int, object)   # (gen, list[Candidate])
    finished_ok: Signal = Signal(object)             # ParetoResult
    failed: Signal = Signal(str)
    progress: Signal = Signal(int, int)              # (current, total)

    def __init__(
        self,
        site: Site,
        profile: RegulationProfile,
        opt_params: OptimizationParams,
        fixed=None,
        parent=None,
    ):
        super().__init__(parent)
        self._site = site
        self._profile = profile
        self._opt_params = opt_params
        self._fixed = fixed

    def run(self) -> None:
        """Entry point — executes on the worker thread."""
        total = self._opt_params.n_gen

        def _cb(gen: int, candidates: list[Candidate]) -> None:
            self.generation_ready.emit(gen, candidates)
            self.progress.emit(gen, total)

        try:
            result = run(
                self._site,
                self._profile,
                self._opt_params,
                fixed=self._fixed,
                generation_callback=_cb,
            )
            self.finished_ok.emit(result)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
