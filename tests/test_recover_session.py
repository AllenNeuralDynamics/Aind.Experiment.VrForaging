"""Recovering a VrForaging session from the folder an earlier run left behind.

Recovery runs rarely, after something went wrong, so a break in it goes unnoticed until it is needed. These tests
lay a session folder out the way a run leaves it, reusing the paths clabe writes to (``Launcher.copy_logs`` and
``steps.TRAINER_STATE_PATH``), so a change on either side shows up here.
"""

import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aind_behavior_curriculum import TrainerState
from aind_behavior_services.session import Session
from clabe import steps
from clabe.workflow import RunStatus

from experiments import fip
from experiments import recover_session as recover
from experiments import vr_foraging as vr

from ._workflow_runner import FakeLauncher, execute, skipped_steps
from .integration.conftest import _load_example

CHOOSE_DIRECTORY = "Choose the session directory to recover:"
REPROCESS = "Would you like to run curriculum evaluation and metadata mapping?"
END_TIME = "Enter the session end time in ISO format (YYYY-MM-DDTHH:MM:SSz), e.g: 2024-01-01T12:00:00Z:"
QC = "Would you like to generate a qc report?"
TRANSFER = "Would you like to transfer data?"
# Where `Launcher.copy_logs` puts the run's logs, which hold the inputs the apps were started with.
LOGS = Path("Behavior") / "Logs"


@pytest.fixture
def session() -> Session:
    return Session(subject="test-mouse", experimenter=["operator"])


@pytest.fixture
def trainer_state() -> TrainerState:
    return TrainerState(curriculum=None, stage=None, is_on_curriculum=False, active_policies=None)


@pytest.fixture
def rig() -> Any:
    """The VrForaging example rig (loaded once: it stamps the time it is built)."""
    return _load_example("rig").rig


@pytest.fixture
def session_directory(tmp_path: Path, session: Session, rig: Any, trainer_state: TrainerState) -> Path:
    """A session folder as a finished VrForaging run leaves it."""
    directory = tmp_path / "test-mouse_2026-11-25"
    (directory / LOGS).mkdir(parents=True)
    (directory / LOGS / "session_input.json").write_text(session.model_dump_json(), encoding="utf-8")
    (directory / LOGS / "rig_input.json").write_text(rig.model_dump_json(), encoding="utf-8")
    (directory / steps.TRAINER_STATE_PATH).write_text(trainer_state.model_dump_json(), encoding="utf-8")
    return directory


# Reading the recorded inputs ----------------------------------------------------------------------------------------


def test_reads_back_the_session_rig_and_trainer_state_the_run_started_with(
    session_directory, session, rig, trainer_state
):
    assert recover.recorded_session(session_directory) == session
    # Compared as JSON: the example rig's array-valued fields make model equality unreliable.
    assert recover.recorded_rig(session_directory).model_dump_json() == rig.model_dump_json()
    assert recover.recorded_trainer_state(session_directory) == trainer_state


def test_missing_trainer_state_is_an_error(session_directory):
    (session_directory / steps.TRAINER_STATE_PATH).unlink()
    with pytest.raises(FileNotFoundError):
        recover.recorded_trainer_state(session_directory)


# The workflow -----------------------------------------------------------------------------------------------------


class Harness:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, session_directory: Path) -> None:
        self.log: list[str] = []
        self.launcher = FakeLauncher(session_directory=session_directory, temp_dir=session_directory.parent / "tmp")
        self.curriculum_inputs: dict[str, Any] = {}

        async def curriculum(**inputs: Any) -> str:
            self.curriculum_inputs = inputs
            self.log.append("curriculum")
            return "curriculum-outcome"

        monkeypatch.setattr(recover, "curriculum", curriculum)
        monkeypatch.setattr(
            vr, "run_vr_foraging_mappers", lambda launcher, outcome, end: self.log.append(f"vr mappers {outcome} {end}")
        )
        monkeypatch.setattr(fip, "run_mapper", lambda launcher: self.log.append("fip mapper"))
        monkeypatch.setattr(vr, "run_data_transfer", lambda *args, **kwargs: self.log.append("transfer"))

    def run(self, answers: dict[str, Any]):
        workflow = (
            recover.recover_session.variant("test-recover-session")
            .replace("register", lambda: None)
            .replace("smartsheet", lambda: SimpleNamespace())
            .replace("store", lambda: "store")
        )
        return execute(workflow, answers, launcher=self.launcher)


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch, session_directory: Path) -> Harness:
    return Harness(monkeypatch, session_directory)


def test_reprocesses_the_session_from_its_folder(harness: Harness, session_directory, session, trainer_state):
    run, client = harness.run(
        {
            CHOOSE_DIRECTORY: str(session_directory),
            REPROCESS: True,
            END_TIME: "2026-11-25T12:30:00Z",
            QC: False,
            TRANSFER: True,
        }
    )

    assert run.status is RunStatus.COMPLETED
    assert client.asked == [CHOOSE_DIRECTORY, REPROCESS, END_TIME, QC, TRANSFER]
    # The curriculum is evaluated from what the original run started with, not from today's store.
    assert harness.curriculum_inputs["session"] == session
    assert harness.curriculum_inputs["trainer_state"] == trainer_state
    end_time = datetime.datetime(2026, 11, 25, 12, 30, tzinfo=datetime.UTC)
    assert harness.log == [
        "curriculum",
        f"vr mappers curriculum-outcome {end_time}",
        "fip mapper",
        "transfer",
    ]


def test_can_transfer_without_reprocessing(harness: Harness, session_directory):
    run, client = harness.run({CHOOSE_DIRECTORY: str(session_directory), REPROCESS: False, QC: False, TRANSFER: True})

    assert skipped_steps(run)["reprocess"] == "declined"
    assert END_TIME not in client.asked
    assert harness.log == ["transfer"]


def test_cancelling_the_folder_choice_aborts_before_anything_runs(harness: Harness):
    run, client = harness.run({CHOOSE_DIRECTORY: None})

    assert run.status is RunStatus.ABORTED
    assert client.asked == [CHOOSE_DIRECTORY]
    assert harness.log == []
