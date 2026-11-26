"""The VrForaging workflow, with FIP recording alongside when the session calls for it.

One workflow covers every combination: the ``modalities`` step decides which physiology modalities record, and the
steps of each modality only run when it is on (``when=``). ``vr-foraging-fip`` is the same workflow with that step
fixed.
"""

import datetime
from pathlib import Path
from typing import Any

from aind_behavior_curriculum import TrainerState
from aind_behavior_services.session import Session
from aind_behavior_services.utils import utcnow
from aind_behavior_vr_foraging.data_contract.utils import calculate_consumed_water
from aind_behavior_vr_foraging.rig import AindVrForagingRig
from aind_behavior_vr_foraging.task_logic import AindVrForagingTaskLogic
from aind_physiology_fip.rig import AindPhysioFipRig
from clabe import checks, steps, ui
from clabe.aind.dataverse import DataverseStore
from clabe.aind.smartsheet import SmartsheetScheduleClient
from clabe.apps import AindBehaviorServicesBonsaiApp, CommandResult, CurriculumOutcome
from clabe.launcher import Launcher
from clabe.stores import CompositeStore, Kind, LocalFileStore, Store
from clabe.workflow import GoBack, OnFailure, Workflow, concurrently
from pydantic import BaseModel

from . import fip
from .common import EXIT_PROMPT_TIMEOUT_S, copy_logs, run_data_transfer
from .curriculum_helpers import prepare_past_metrics
from .vr_foraging_helpers import (
    ByAnimalManipulatorModifier,
    add_scientific_contact_or_warn,
    disable_sniff_detector_without_thermistor,
    run_data_qc,
    run_vr_foraging_mappers,
)

CONFIG_LIBRARY = Path(r"\\allen\aind\scratch\AindBehavior.db\AindVrForaging")
RIG = Kind.from_rig(AindVrForagingRig)
_TRAINER_STATE = Kind.from_trainer_state()
_TASK_NAME = AindVrForagingTaskLogic.model_fields["name"].default
assert isinstance(_TASK_NAME, str), "AindVrForagingTaskLogic must define a default task name."
_BONSAI_WORKFLOW = Path(r"./Aind.Behavior.VrForaging/src/main.bonsai")
_BONSAI_EXECUTABLE = Path(r"./Aind.Behavior.VrForaging/.bonsai/bonsai.exe")
# Shorter sessions are not submitted for upload, to keep sporadic sessions out of the data pipeline.
_MIN_SESSION_DURATION = datetime.timedelta(minutes=5)


class Modalities(BaseModel, frozen=True):
    """The physiology modalities recording with the behavior."""

    fip: bool = False


def records_fip(modalities: Modalities) -> bool:
    return modalities.fip


def behavior_store(session: Session) -> Store:
    """The configuration store, scoped to the animal and the task."""
    return CompositeStore(
        default=LocalFileStore(CONFIG_LIBRARY),
        routes={_TRAINER_STATE.name: DataverseStore()},
    ).scoped(subject=session.subject, task_name=_TASK_NAME)


def behavior_app(
    launcher: Launcher, rig: AindVrForagingRig, session: Session, task: AindVrForagingTaskLogic
) -> AindBehaviorServicesBonsaiApp:
    """The VrForaging Bonsai workflow."""
    return AindBehaviorServicesBonsaiApp(
        workflow=_BONSAI_WORKFLOW,
        executable=_BONSAI_EXECUTABLE,
        temp_directory=launcher.temp_dir,
        rig=rig,
        session=session,
        task=task,
    )


vr_foraging = Workflow("vr-foraging", order=0, exit_prompt_timeout=EXIT_PROMPT_TIMEOUT_S)

# Session ----------------------------------------------------------------------------------------------------------

vr_foraging.step(steps.session(), name="subject", title="Animal")


@vr_foraging.step
def smartsheet() -> SmartsheetScheduleClient:
    return SmartsheetScheduleClient()


@vr_foraging.step
def session(subject: Session, smartsheet: SmartsheetScheduleClient) -> Session:
    """The session, with the animal's scientific contact when the Smartsheet has one."""
    return add_scientific_contact_or_warn(smartsheet, subject)


@vr_foraging.step
def modalities() -> Modalities:
    """VrForaging alone. The variant at the end of this module fixes this step to add FIP."""
    return Modalities()


# Rig and task configuration ---------------------------------------------------------------------------------------


@vr_foraging.step
def store(session: Session) -> Store:
    return behavior_store(session)


@vr_foraging.step
def trainer_state(store: Store) -> TrainerState:
    """The animal's current stage."""
    state = store.resolve(_TRAINER_STATE)
    assert state.stage is not None
    return state


@vr_foraging.step
def task(trainer_state: TrainerState) -> AindVrForagingTaskLogic:
    """The task logic of the animal's current stage."""
    assert trainer_state.stage is not None
    return AindVrForagingTaskLogic.model_validate_json(trainer_state.stage.task.model_dump_json())


@vr_foraging.step(title="VR Foraging rig")
def rig(store: Store, session: Session, smartsheet: SmartsheetScheduleClient) -> AindVrForagingRig:
    """The rig, without the sniff detector unless the animal has a thermistor."""
    vr_rig = store.resolve(RIG)
    disable_sniff_detector_without_thermistor(smartsheet, session, vr_rig)
    return vr_rig


vr_foraging.step(fip.resolve_rig, name="fip_rig", title="FIP rig", when=records_fip)


# Review and preflight checks --------------------------------------------------------------------------------------


@vr_foraging.step
def confirm_session(session: Session, trainer_state: TrainerState, modalities: Modalities) -> None:
    """Shows who runs which animal, stage and modalities; "Go back" asks for the animal again."""
    summary = {
        "Mouse": session.subject,
        "Experimenter": ", ".join(session.experimenter) if session.experimenter else None,
        "Curriculum": trainer_state.curriculum.name if trainer_state.curriculum else None,
        "Stage": trainer_state.stage.name if trainer_state.stage else None,
        "Physiology": "FIP" if modalities.fip else None,
    }
    confirmed = ui.table(
        ["Item", "Value"],
        [[key, value] for key, value in summary.items()],
        title="Confirm Session Information",
        prompt="Is this information correct?",
        confirm_label="Looks good",
        cancel_label="Go back",
    )
    if not confirmed:
        raise GoBack("subject")


vr_foraging.step(steps.preflight(checks.free_space(2e11)))
vr_foraging.step(steps.register())


@vr_foraging.step(title="Manipulator position")
def manipulator(launcher: Launcher, store: Store, session: Session) -> ByAnimalManipulatorModifier:
    """Where the animal's last session left the manipulator."""
    return ByAnimalManipulatorModifier(subject=session.subject, store=store, launcher=launcher)


@vr_foraging.step
def positioned_rig(rig: AindVrForagingRig, manipulator: ByAnimalManipulatorModifier) -> AindVrForagingRig:
    """The rig that runs, with the manipulator starting where the animal's last session left it."""
    return manipulator.inject(rig)


# Acquisition ------------------------------------------------------------------------------------------------------


@vr_foraging.step(cancellable=True, on_failure=OnFailure.RETRY | OnFailure.ABORT)
async def acquire(
    launcher: Launcher,
    session: Session,
    positioned_rig: AindVrForagingRig,
    task: AindVrForagingTaskLogic,
    fip_rig: AindPhysioFipRig | None,
) -> dict[str, Any]:
    """Runs VrForaging, and FIP alongside when it records; if either fails, Retry or Abort."""
    apps = {"behavior": behavior_app(launcher, positioned_rig, session, task)}
    if fip_rig is not None:
        apps["fip"] = fip.fip_app(launcher, fip_rig, session)
    return await concurrently(**{name: app.run_async for name, app in apps.items()})


@vr_foraging.step
def session_end_time() -> datetime.datetime:
    return utcnow()


# After the session ------------------------------------------------------------------------------------------------


@vr_foraging.step(on_error="warn")
def save_manipulator_position(manipulator: ByAnimalManipulatorModifier) -> None:
    """Stores where the manipulator ended, for the animal's next session."""
    manipulator.update()


def write_past_metrics(session: Session, trainer_state: TrainerState, launcher: Launcher) -> None:
    """Writes the animal's recent sessions where history-dependent curricula read them. Never fails."""
    assert trainer_state.stage is not None
    prepare_past_metrics(
        launcher.session_directory,
        subject=session.subject,
        before=session.date,
        task_name=trainer_state.stage.task.name,
    )


vr_foraging.step(steps.curriculum(before=write_past_metrics))


vr_foraging.step(steps.waterlog(lambda launcher: calculate_consumed_water(launcher.session_directory)))


@vr_foraging.step
def mappers(launcher: Launcher, curriculum: CurriculumOutcome | None, session_end_time: datetime.datetime) -> None:
    """Maps the VrForaging data to the data schema."""
    run_vr_foraging_mappers(launcher, curriculum, session_end_time)


@vr_foraging.step(title="FIP mapper", when=records_fip)
def fip_mapper(launcher: Launcher) -> None:
    fip.run_mapper(launcher)


@vr_foraging.step(title="Data QC", optional="Would you like to generate a qc report?", optional_default=False)
def qc(launcher: Launcher) -> None:
    """Generates the VrForaging QC report, and FIP's when the session has FIP data."""
    run_data_qc(launcher)
    fip.run_data_qc(launcher)


vr_foraging.step(copy_logs)


@vr_foraging.step(optional="Would you like to transfer data?")
def transfer(launcher: Launcher, session: Session, smartsheet: SmartsheetScheduleClient) -> None:
    """Hands the session to the watchdog."""
    run_data_transfer(launcher, session, smartsheet_client=smartsheet, min_session_duration=_MIN_SESSION_DURATION)


# Variants, until the modalities are decided per session -----------------------------------------------------------


def with_fip() -> Modalities:
    """VrForaging and FIP."""
    return Modalities(fip=True)


vr_foraging_fip = vr_foraging.variant("vr-foraging-fip", order=1).replace("modalities", with_fip)


# Calibration: the rig alone, nothing recorded ---------------------------------------------------------------------

calibration = Workflow("calibration", order=2, exit_prompt_timeout=EXIT_PROMPT_TIMEOUT_S)


@calibration.step(name="session")
def calibration_session() -> Session:
    return Session(
        subject="CALIBRATION",
        experiment="CALIBRATION",
        date=utcnow(),
        allow_dirty_repo=True,
        notes="Session for rig calibration. No actual experiment data will be recorded.",
    )


@calibration.step(name="rig", title="VR Foraging rig")
def calibration_rig() -> AindVrForagingRig:
    return LocalFileStore(CONFIG_LIBRARY).resolve(RIG)


calibration.step(steps.register())


@calibration.step(cancellable=True)
async def calibrate(launcher: Launcher, session: Session, rig: AindVrForagingRig) -> CommandResult:
    return await behavior_app(launcher, rig, session, AindVrForagingTaskLogic()).run_async()
