"""The workflows ``main.py`` registers, and the VrForaging QC step that shows its report in the run's page."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aind_behavior_vr_foraging.data_qc import data_qc
from clabe.workflow import Abort, Workflow
from contraqctor.qc import reporters

import main
from experiments import vr_foraging_helpers

WORKFLOWS: list[Workflow] = [getattr(main, name) for name in main.__all__]


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda workflow: workflow.name)
def test_every_workflow_has_a_description(workflow: Workflow):
    # The daemon's page shows it under the workflow's name.
    assert workflow.description


def test_descriptions_are_distinct():
    # A variant copies its workflow's description unless it is given its own.
    descriptions = [workflow.description for workflow in WORKFLOWS]
    assert len(set(descriptions)) == len(descriptions)


class QcHarness:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, qc_fails: bool = False) -> None:
        self.launcher = SimpleNamespace(session_directory=tmp_path)
        self.shown: list[tuple[Path, dict[str, Any]]] = []
        self.qc_fails = qc_fails

        def make_qc_runner(dataset: Any) -> Any:
            if self.qc_fails:
                raise RuntimeError("bad dataset")
            return SimpleNamespace(run_all_with_progress=lambda reporter: None)

        monkeypatch.setattr(vr_foraging_helpers.data_contract, "dataset", lambda path: "dataset")
        monkeypatch.setattr(data_qc, "make_qc_runner", make_qc_runner)
        monkeypatch.setattr(reporters, "HtmlReporter", lambda output_path: None)
        monkeypatch.setattr(vr_foraging_helpers.ui, "notify", lambda *args, **kwargs: None)
        monkeypatch.setattr(
            vr_foraging_helpers.ui, "html", lambda source, **kwargs: self.shown.append((source, kwargs))
        )


def test_qc_report_is_shown_in_the_runs_page(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    harness = QcHarness(monkeypatch, tmp_path)

    vr_foraging_helpers.run_data_qc(harness.launcher)  # type: ignore[arg-type]

    assert [source for source, _ in harness.shown] == [tmp_path / "Behavior" / "Logs" / "qc_report.html"]


def test_a_failed_qc_is_reported_and_shows_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    harness = QcHarness(monkeypatch, tmp_path, qc_fails=True)

    vr_foraging_helpers.run_data_qc(harness.launcher)  # type: ignore[arg-type]  # does not raise

    assert harness.shown == []


def test_aborting_while_the_report_is_shown_aborts_the_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    harness = QcHarness(monkeypatch, tmp_path)

    def abort(source: Path, **kwargs: Any) -> None:
        raise Abort("operator aborted")

    monkeypatch.setattr(vr_foraging_helpers.ui, "html", abort)

    # QC failures are swallowed, but an Abort (an Exception too) must reach the workflow.
    with pytest.raises(Abort):
        vr_foraging_helpers.run_data_qc(harness.launcher)  # type: ignore[arg-type]
