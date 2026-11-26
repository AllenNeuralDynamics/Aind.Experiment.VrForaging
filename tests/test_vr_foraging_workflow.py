"""The VrForaging workflows run end to end, with the rig, the services and the apps faked.

What is faked is what needs a rig or the network: the animal prompt, Smartsheet, the configuration store, the clabe
preflight/register/curriculum/waterlog steps, and the Bonsai apps. Everything else runs as written: the ``when=``
conditions, the confirmation table and its "Go back", the optional questions, and the steps that wire the modalities
together (``acquire``, the mappers).
"""

import datetime
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aind_behavior_services.session import Session
from clabe.workflow import RunStatus, Workflow
from clabe.workflow.events import PromptOpened

from experiments import fip
from experiments import vr_foraging as vr
from experiments.vr_foraging_helpers import add_scientific_contact_or_warn, disable_sniff_detector_without_thermistor

from ._workflow_runner import FakeLauncher, execute, skipped_steps, started_steps

CONFIRM = "Is this information correct?"
QC = "Would you like to generate a qc report?"
TRANSFER = "Would you like to transfer data?"
# The operator's answers for a session that goes as planned and is not uploaded.
NO_UPLOAD = {CONFIRM: True, QC: False, TRANSFER: False}

FIP_STEPS = {"fip_rig", "fip_mapper"}


class FakeApp:
    """An app whose next ``crashes()`` runs crash. A branch retry calls ``run_async`` of the same app again, as it
    does with the real Bonsai app, so whether a run crashes is decided per call."""

    def __init__(self, name: str, log: list[str], crashes: Callable[[], bool]) -> None:
        self.name, self.log, self.crashes = name, log, crashes

    async def run_async(self) -> str:
        self.log.append(f"{self.name} runs")
        if self.crashes():
            raise RuntimeError(f"{self.name} crashed")
        return self.name


class FakeStore:
    def __init__(self, rig: Any, trainer_state: Any) -> None:
        self._values = {vr.RIG.name: rig, "trainer_state": trainer_state}

    def resolve(self, kind: Any) -> Any:
        return self._values[kind.name]


class FakeSmartsheet:
    def __init__(self, *, has_thermistor: str | None = "true") -> None:
        self.has_thermistor = has_thermistor

    def add_scientific_contact(self, session: Session) -> Session:
        return session.model_copy(update={"experimenter": [*session.experimenter, "scientific-contact"]})

    def get_row(self, subject: str) -> SimpleNamespace:
        return SimpleNamespace(has_thermistor=self.has_thermistor)


class FakeManipulator:
    def __init__(self, log: list[str]) -> None:
        self.log = log

    def inject(self, rig: Any) -> Any:
        self.log.append("manipulator positioned")
        return rig

    def update(self) -> None:
        self.log.append("manipulator position saved")


class Harness:
    """Builds a test copy of a VrForaging workflow whose rig- and network-bound parts are fakes, and records what
    the remaining (real) steps did in ``log``."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.log: list[str] = []
        self.launcher = FakeLauncher(session_directory=tmp_path / "session", temp_dir=tmp_path / "temp")
        self.session = Session(subject="test-mouse", experimenter=["operator"])
        self.rig = SimpleNamespace(harp_sniff_detector="sniff-detector")
        self.trainer_state = SimpleNamespace(
            curriculum=SimpleNamespace(name="vr-curriculum"), stage=SimpleNamespace(name="stage-1")
        )
        self.smartsheet = FakeSmartsheet()
        self.apps: dict[str, dict[str, Any]] = {}
        self.failures: dict[str, int] = {}  # how many of an app's next runs crash
        self.transfers: list[dict[str, Any]] = []
        self.subject_prompts = 0

        def behavior_app(launcher: Any, rig: Any, session: Session, task: Any) -> FakeApp:
            self.apps["behavior"] = {"rig": rig, "session": session, "task": task}
            return FakeApp("behavior", self.log, lambda: self._crashes("behavior"))

        def fip_app(launcher: Any, rig: Any, session: Session) -> FakeApp:
            self.apps["fip"] = {"rig": rig, "session": session}
            return FakeApp("fip", self.log, lambda: self._crashes("fip"))

        monkeypatch.setattr(vr, "behavior_app", behavior_app)
        monkeypatch.setattr(fip, "fip_app", fip_app)
        monkeypatch.setattr(vr, "run_vr_foraging_mappers", lambda *args: self.log.append("vr mappers"))
        monkeypatch.setattr(fip, "run_mapper", lambda launcher: self.log.append("fip mapper"))
        monkeypatch.setattr(vr, "run_data_qc", lambda launcher: self.log.append("vr qc"))
        monkeypatch.setattr(fip, "run_data_qc", lambda launcher: self.log.append("fip qc"))
        monkeypatch.setattr(vr, "run_data_transfer", lambda *args, **kwargs: self.transfers.append(kwargs))

    def _crashes(self, app: str) -> bool:
        if self.failures.get(app, 0) <= 0:
            return False
        self.failures[app] -= 1
        return True

    def _subject(self) -> Session:
        self.subject_prompts += 1
        return self.session

    def build(self, workflow: Workflow) -> Workflow:
        """A copy of ``workflow`` with the rig- and network-bound steps replaced (keeping their ``when=``)."""
        return (
            workflow.variant(f"test-{workflow.name}")
            .replace("subject", self._subject)
            .replace("smartsheet", lambda: self.smartsheet)
            .replace("store", lambda: FakeStore(self.rig, self.trainer_state))
            .replace("task", lambda: "task-logic")
            .replace("fip_rig", lambda: "fip-rig")
            .replace("preflight", lambda: None)
            .replace("register", lambda: None)
            .replace("manipulator", lambda: FakeManipulator(self.log))
            .replace("curriculum", lambda: self.log.append("curriculum") or None)
            .replace("waterlog", lambda: None)
        )

    def run(self, workflow: Workflow, answers: dict[str, Any] | None = None):
        return execute(self.build(workflow), NO_UPLOAD if answers is None else answers, launcher=self.launcher)


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Harness:
    return Harness(monkeypatch, tmp_path)


# Which steps each variant runs ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("workflow", "skipped", "apps"),
    [
        (vr.vr_foraging, FIP_STEPS, {"behavior"}),
        (vr.vr_foraging_fip, set(), {"behavior", "fip"}),
    ],
    ids=["behavior-only", "fip"],
)
def test_each_variant_runs_only_its_modalities_steps(harness: Harness, workflow, skipped, apps):
    run, _ = harness.run(workflow)

    assert run.status is RunStatus.COMPLETED
    conditional = {step for step, reason in skipped_steps(run).items() if reason == "condition"}
    assert conditional == skipped
    assert set(run.output("acquire")) == apps
    assert set(harness.apps) == apps


def test_behavior_only_session_never_touches_fip(harness: Harness):
    run, _ = harness.run(vr.vr_foraging)

    # A skipped step's output is None, which is what `acquire` checks to leave FIP out.
    assert run.output("fip_rig") is None
    assert not any("fip" in entry for entry in harness.log)
    assert harness.launcher.session_directories == []


def test_fip_session_runs_fip_alongside_behavior_with_the_same_session(harness: Harness):
    harness.run(vr.vr_foraging_fip)

    assert harness.apps["fip"]["rig"] == "fip-rig"
    assert harness.apps["fip"]["session"] == harness.apps["behavior"]["session"]
    assert harness.log.index("fip mapper") > harness.log.index("vr mappers")


# What the operator is asked ---------------------------------------------------------------------------------------


def test_operator_questions_and_their_order(harness: Harness):
    _, client = harness.run(vr.vr_foraging_fip)

    assert client.asked == [CONFIRM, QC, TRANSFER]


def test_go_back_at_the_confirmation_asks_for_the_animal_again(harness: Harness):
    run, client = harness.run(vr.vr_foraging, {**NO_UPLOAD, CONFIRM: [False, True]})

    assert run.status is RunStatus.COMPLETED
    assert harness.subject_prompts == 2
    assert client.asked.count(CONFIRM) == 2
    assert harness.log.count("behavior runs") == 1  # nothing ran before the operator confirmed


def test_confirmation_shows_the_session_summary(harness: Harness):
    run, _ = harness.run(vr.vr_foraging_fip)

    table = next(e for e in run.events.since(0) if isinstance(e, PromptOpened) and e.spec.get("prompt") == CONFIRM)
    assert dict(table.spec["rows"]) == {
        "Mouse": "test-mouse",
        "Experimenter": "operator, scientific-contact",
        "Curriculum": "vr-curriculum",
        "Stage": "stage-1",
        "Physiology": "FIP",
    }


def test_qc_is_off_unless_asked_for(harness: Harness):
    run, _ = harness.run(vr.vr_foraging_fip)
    assert skipped_steps(run)["qc"] == "declined"
    assert "vr qc" not in harness.log and "fip qc" not in harness.log


def test_qc_runs_behavior_and_fip_qc_when_asked_for(harness: Harness):
    harness.run(vr.vr_foraging_fip, {**NO_UPLOAD, QC: True})
    assert harness.log.index("vr qc") < harness.log.index("fip qc")


def test_transfer_uses_smartsheet_and_the_minimum_session_duration(harness: Harness):
    harness.run(vr.vr_foraging, {**NO_UPLOAD, TRANSFER: True})

    assert len(harness.transfers) == 1
    assert harness.transfers[0]["smartsheet_client"] is harness.smartsheet
    assert harness.transfers[0]["min_session_duration"] == datetime.timedelta(minutes=5)


def test_logs_are_copied_even_when_the_transfer_is_declined(harness: Harness):
    run, _ = harness.run(vr.vr_foraging)
    assert harness.launcher.copied_logs == 1
    assert "copy_logs" in started_steps(run) and skipped_steps(run)["transfer"] == "declined"


# A failed acquisition ---------------------------------------------------------------------------------------------


# The apps run as `concurrently` branches. When one crashes, the operator is asked, for that app alone
# (``acquire.<app>.failed``): Retry it, Continue without it (its result is None), or Stop all. Stop all fails the
# step, which then offers Retry / Abort for the whole acquisition (``acquire.failed``).


@pytest.mark.parametrize("failing", ["behavior", "fip"])
def test_stopping_a_crashed_acquisition_skips_curriculum_mappers_and_transfer(harness: Harness, failing: str):
    harness.failures[failing] = 1

    run, client = harness.run(
        vr.vr_foraging_fip, {CONFIRM: True, f"acquire.{failing}.failed": "stop", "acquire.failed": "abort"}
    )

    assert run.status is RunStatus.FAILED
    # Above all, the curriculum must not run: it would move the animal on from a session that did not happen.
    assert "curriculum" not in harness.log
    assert "vr mappers" not in harness.log and harness.transfers == []
    assert TRANSFER not in client.asked


def test_retrying_a_crashed_behavior_restarts_it_alone(harness: Harness):
    harness.failures["behavior"] = 1

    run, _ = harness.run(vr.vr_foraging_fip, {**NO_UPLOAD, "acquire.behavior.failed": "retry"})

    assert run.status is RunStatus.COMPLETED
    assert run.output("acquire") == {"behavior": "behavior", "fip": "fip"}
    assert harness.log.count("behavior runs") == 2
    assert harness.log.count("fip runs") == 1  # FIP keeps its one recording while the behavior restarts
    assert harness.log.count("curriculum") == 1


def test_retrying_the_whole_acquisition_restarts_every_app(harness: Harness):
    harness.failures["behavior"] = 1

    run, _ = harness.run(
        vr.vr_foraging_fip, {**NO_UPLOAD, "acquire.behavior.failed": "stop", "acquire.failed": "retry"}
    )

    assert run.status is RunStatus.COMPLETED
    assert harness.log.count("behavior runs") == 2 and harness.log.count("fip runs") == 2


@pytest.mark.xfail(
    strict=True,
    reason=(
        "'Continue without behavior' finishes the session as if it had run: the curriculum is evaluated (and its "
        "suggestion uploaded) and the behavior is mapped, from a session whose behavior app crashed. It is offered "
        "even when the behavior is the only app. One fix: in `acquire`, raise when the behavior's result is None, so "
        "only Retry / Abort remain."
    ),
)
@pytest.mark.parametrize("workflow", [vr.vr_foraging, vr.vr_foraging_fip], ids=["behavior-only", "fip"])
def test_continuing_without_the_behavior_does_not_evaluate_the_curriculum(harness: Harness, workflow: Workflow):
    harness.failures["behavior"] = 1

    harness.run(workflow, {**NO_UPLOAD, "acquire.behavior.failed": "skip", "acquire.failed": "abort"})

    assert "curriculum" not in harness.log


# Smartsheet-driven helpers ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("has_thermistor", "keeps_sniff_detector"),
    [("true", True), ("True ", True), ("false", False), ("", False), (None, False), ("yes", False)],
)
def test_sniff_detector_is_kept_only_for_a_confirmed_thermistor(has_thermistor, keeps_sniff_detector):
    rig = SimpleNamespace(harp_sniff_detector="sniff-detector")
    disable_sniff_detector_without_thermistor(FakeSmartsheet(has_thermistor=has_thermistor), Session(subject="m"), rig)
    assert (rig.harp_sniff_detector is not None) is keeps_sniff_detector


def test_sniff_detector_is_removed_when_the_animal_has_no_smartsheet_row():
    class NoRow(FakeSmartsheet):
        def get_row(self, subject: str) -> None:
            return None

    rig = SimpleNamespace(harp_sniff_detector="sniff-detector")
    disable_sniff_detector_without_thermistor(NoRow(), Session(subject="m"), rig)
    assert rig.harp_sniff_detector is None


def test_workflow_rig_has_no_sniff_detector_without_a_thermistor(harness: Harness):
    harness.smartsheet = FakeSmartsheet(has_thermistor="false")
    run, _ = harness.run(vr.vr_foraging)
    assert run.output("positioned_rig").harp_sniff_detector is None


def test_a_missing_scientific_contact_does_not_stop_the_session():
    class NoContact(FakeSmartsheet):
        def add_scientific_contact(self, session: Session) -> Session:
            raise ValueError("subject not in the smartsheet")

    session = Session(subject="m", experimenter=["operator"])
    assert add_scientific_contact_or_warn(NoContact(), session) is session
