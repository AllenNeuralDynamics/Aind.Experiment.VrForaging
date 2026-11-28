"""Recovers a VrForaging session whose Bonsai workflow(s) already completed (e.g. after a launcher crash).

It loads the session from its directory, then runs what ``vr-foraging`` runs after the session: curriculum, data
mappers, QC and transfer.
"""

from pathlib import Path

from aind_behavior_curriculum import TrainerState
from aind_behavior_services.session import Session
from aind_behavior_vr_foraging.rig import AindVrForagingRig
from clabe import steps, ui
from clabe.launcher import Launcher
from clabe.stores import Store
from clabe.workflow import Abort, Workflow

from . import fip
from . import vr_foraging as vr
from .common import EXIT_PROMPT_TIMEOUT_S, ask_session_end_time
from .curriculum_helpers import prepare_past_metrics

recover_session = Workflow(
    "recover-session",
    description="Reprocesses a finished session from its folder (e.g. after a launcher crash): curriculum, "
    "mapping, QC and transfer.",
    order=4,
    exit_prompt_timeout=EXIT_PROMPT_TIMEOUT_S,
)


@recover_session.step
def session_directory() -> Path:
    path = ui.path("Choose the session directory to recover:", kind="dir", field="session_directory")
    if path is None:
        raise Abort("Session recovery cancelled.")
    return path


@recover_session.step(name="session")
def recorded_session(session_directory: Path) -> Session:
    return Session.model_validate_json((session_directory / "behavior/Logs/session_input.json").read_text("utf-8"))


@recover_session.step(name="rig", title="VR Foraging rig")
def recorded_rig(session_directory: Path) -> AindVrForagingRig:
    return AindVrForagingRig.model_validate_json(
        (session_directory / "behavior/Logs/rig_input.json").read_text("utf-8")
    )


@recover_session.step(name="trainer_state")
def recorded_trainer_state(session_directory: Path) -> TrainerState:
    """The trainer state the session started from, saved by the original run."""
    files = sorted((session_directory / "behavior").glob("trainer_state*.json"))
    if not files:
        raise FileNotFoundError("Trainer state file not found.")
    return TrainerState.model_validate_json(files[0].read_text(encoding="utf-8"))


recover_session.step(steps.register())
recover_session.step(vr.smartsheet)
recover_session.step(vr.store)


def keep_past_metrics(session: Session, trainer_state: TrainerState, launcher: Launcher) -> None:
    """The history the original run saw, so the curriculum evaluates what it evaluated. Never fails."""
    assert trainer_state.stage is not None
    prepare_past_metrics(
        launcher.session_directory,
        subject=session.subject,
        before=session.date,
        task_name=trainer_state.stage.task.name,
        keep_existing=True,
    )


curriculum = steps.curriculum(
    before=keep_past_metrics,
    upload=lambda: ui.confirm(
        "Would you like to upload the curriculum suggestion? This overwrites the subject's current trainer state.",
        default=True,
    ),
)


@recover_session.step(
    title="Curriculum and mapping", optional="Would you like to run curriculum evaluation and metadata mapping?"
)
async def reprocess(
    launcher: Launcher,
    store: Store,
    session: Session,
    trainer_state: TrainerState,
) -> None:
    """Evaluates the curriculum on the session history the original run saw (uploading the suggestion if asked to),
    then maps every modality's data."""
    outcome = await curriculum(launcher=launcher, session=session, store=store, trainer_state=trainer_state)
    vr.mappers(launcher, outcome, ask_session_end_time())
    fip.run_mapper(launcher)


recover_session.step(vr.vr_foraging.get("qc"))


recover_session.step(vr.vr_foraging.get("transfer"))
