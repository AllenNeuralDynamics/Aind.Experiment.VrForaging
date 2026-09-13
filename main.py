"""CLABE launcher script for Aind.Behavior.VrForaging + Physiology experiments.

Run with the generic clabe CLI, e.g.:
    clabe run main.py
    python -m clabe.cli run main.py
"""

import asyncio
import datetime
import logging
from pathlib import Path

import aind_physiology_fip.rig
from aind_behavior_curriculum import TrainerState
from aind_behavior_services.session import Session
from aind_behavior_services.utils import utcnow
from aind_behavior_vr_foraging.data_contract.utils import calculate_consumed_water
from aind_behavior_vr_foraging.rig import AindVrForagingRig
from aind_behavior_vr_foraging.task_logic import AindVrForagingTaskLogic
from clabe import aind_apps, resource_monitor, ui
from clabe.apps import AindBehaviorServicesBonsaiApp, CurriculumSettings
from clabe.launcher import Launcher, experiment
from clabe.logging import otel
from clabe.session import SessionBuilder
from clabe.stores import CompositeStore, Kind, LocalFileStore, Store
from clabe.stores.dataverse import DataverseStore

from common import (
    ByAnimalManipulatorModifier,
    confirm_session_info,
    run_curriculum_if_applicable,
    run_data_qc,
    run_data_transfer,
    run_fip_mapper,
    run_vr_foraging_mappers,
)

logger = logging.getLogger(__name__)

_VR_CONFIG_LIBRARY = Path(r"\\allen\aind\scratch\AindBehavior.db\AindVrForaging")
_FIP_CONFIG_LIBRARY = Path(r"\\allen\aind\scratch\AindBehavior.db\AindPhysiologyFip")
_VR_RIG = Kind.from_rig(AindVrForagingRig)
_FIP_RIG = Kind.from_rig(aind_physiology_fip.rig.AindPhysioFipRig)
_TRAINER_STATE = Kind.from_trainer_state()
_TASK_NAME = AindVrForagingTaskLogic.model_fields["name"].default
assert isinstance(_TASK_NAME, str), "AindVrForagingTaskLogic must define a default task name."


def _behavior_store(session: Session) -> Store:
    return CompositeStore(
        default=LocalFileStore(_VR_CONFIG_LIBRARY),
        routes={_TRAINER_STATE.name: DataverseStore()},
    ).scoped(subject=session.subject, task_name=_TASK_NAME)


async def _run_vr_foraging_experiment(launcher: Launcher, *, with_fip: bool) -> None:
    """Shared implementation for the ``vr-foraging`` and ``vr-foraging-fip`` experiments."""
    # Start experiment setup
    session = SessionBuilder(launcher).build()
    store = _behavior_store(session)

    # Fetch the task settings
    trainer_state = store.resolve(_TRAINER_STATE)
    assert trainer_state.stage is not None
    task_logic = AindVrForagingTaskLogic.model_validate_json(trainer_state.stage.task.model_dump_json())

    # Fetch rig settings
    logger.info("Pick VR Foraging rig...")
    rig = store.resolve(_VR_RIG)
    fip_rig = None
    if with_fip:
        logger.info("Pick FIP rig...")
        fip_rig = LocalFileStore(_FIP_CONFIG_LIBRARY).resolve(_FIP_RIG)

    if not confirm_session_info(launcher, session, trainer_state):
        ui.notify("Session information not confirmed. Aborting.", ui.MessageLevel.WARNING)
        otel.event("session-aborted")
        return

    launcher.register_session(session, rig.data_directory)

    resource_monitor.ResourceMonitor(
        constrains=[
            resource_monitor.available_storage_constraint_factory(rig.data_directory, 2e11),
        ]
    ).run()

    input_trainer_state_path = launcher.session_directory / "behavior" / "trainer_state.json"
    input_trainer_state_path.parent.mkdir(parents=True, exist_ok=True)
    input_trainer_state_path.write_text(trainer_state.model_dump_json(indent=2), encoding="utf-8")

    # Post-fetching modifications
    manipulator_modifier = ByAnimalManipulatorModifier(
        subject=session.subject,
        store=store,
        launcher=launcher,
    )
    manipulator_modifier.inject(rig)

    # Run the VrForaging workflow, and FIP concurrently if enabled
    bonsai_app = AindBehaviorServicesBonsaiApp(
        workflow=Path(r"./Aind.Behavior.VrForaging/src/main.bonsai"),
        executable=Path(r"./Aind.Behavior.VrForaging/.bonsai/bonsai.exe"),
        temp_directory=launcher.temp_dir,
        rig=rig,
        session=session,
        task=task_logic,
    )
    if fip_rig is not None:
        fip_app = AindBehaviorServicesBonsaiApp(
            workflow=Path(r"./Aind.Physiology.Fip/src/main.bonsai"),
            executable=Path(r"./Aind.Physiology.Fip/bonsai/bonsai.exe"),
            temp_directory=launcher.temp_dir,
            rig=fip_rig,
            session=session,
        )
        await asyncio.gather(bonsai_app.run_async(), fip_app.run_async())
    else:
        await bonsai_app.run_async()

    # Update manipulator initial position for next session
    try:
        manipulator_modifier.update()
    except Exception as e:
        logger.error("Failed to update manipulator initial position: %s", e)
        ui.notify(f"Failed to update manipulator position: {e}", ui.MessageLevel.WARNING)
        otel.record_exception(e)

    # Curriculum
    (
        _,
        suggestion_path,
        curriculum_settings,
    ) = await run_curriculum_if_applicable(store, trainer_state, input_trainer_state_path, launcher)

    # Waterlog
    try:
        consumed_water = calculate_consumed_water(launcher.session_directory)
        aind_apps.WaterlogApp(
            settings=aind_apps.WaterlogSettings(
                username=session.experimenter[0] if session.experimenter else None,
                mouse_id=session.subject,
                earned_water=consumed_water,
            )
        ).run()
    except Exception as e:
        logger.error("Error while attempting to waterlog: %s", e)
        otel.record_exception(e)

    # Mappers
    run_vr_foraging_mappers(launcher, suggestion_path, curriculum_settings, utcnow())
    if with_fip:
        run_fip_mapper(launcher)
    ui.notify("Data mapping complete.", ui.MessageLevel.SUCCESS)

    # Data QC
    run_data_qc(launcher)

    # Watchdog
    launcher.copy_logs()
    run_data_transfer(launcher, session)


@experiment(name="vr-foraging", order=0)
async def vr_foraging_protocol(launcher: Launcher) -> None:
    """Run VrForaging on its own, without FIP."""
    await _run_vr_foraging_experiment(launcher, with_fip=False)


@experiment(name="vr-foraging-fip", order=1)
async def vr_foraging_fip_protocol(launcher: Launcher) -> None:
    """Run VrForaging and FIP concurrently as a single combined session."""
    await _run_vr_foraging_experiment(launcher, with_fip=True)


@experiment(name="calibration", order=2)
async def calibration_protocol(launcher: Launcher) -> None:
    """Run only the VrForaging rig, for calibration purposes. No data is recorded."""
    session = Session(
        subject="CALIBRATION",
        experiment="CALIBRATION",
        date=utcnow(),
        allow_dirty_repo=True,
        notes="Session for rig calibration. No actual experiment data will be recorded.",
    )

    rig = LocalFileStore(_VR_CONFIG_LIBRARY).resolve(_VR_RIG)
    launcher.register_session(session, rig.data_directory)

    bonsai_app = AindBehaviorServicesBonsaiApp(
        workflow=Path(r"./Aind.Behavior.VrForaging/src/main.bonsai"),
        executable=Path(r"./Aind.Behavior.VrForaging/.bonsai/bonsai.exe"),
        temp_directory=launcher.temp_dir,
        task=AindVrForagingTaskLogic(),
        rig=rig,
        session=session,
    )
    await bonsai_app.run_async()
    ui.notify("Calibration protocol completed successfully.", ui.MessageLevel.SUCCESS)


@experiment(name="recover-session", order=3)
async def recover_session(launcher: Launcher) -> None:
    """Re-run curriculum evaluation, data mapping, QC and transfer for a session
    whose bonsai workflow(s) already completed (e.g. after a launcher crash)."""
    session_path = launcher.frontend.prompt_path(
        ui.PathRequest(
            label="Choose the session directory to recover:",
            kind="dir",
            field="session_directory",
        )
    )
    if session_path is None:
        ui.notify("Session recovery cancelled.", ui.MessageLevel.WARNING)
        return

    session_model = Session.model_validate_json(
        (session_path / "behavior/Logs/session_input.json").read_text(encoding="utf-8")
    )
    rig_model = AindVrForagingRig.model_validate_json(
        (session_path / "behavior/Logs/rig_input.json").read_text(encoding="utf-8")
    )
    trainer_state_files = list((session_path / "behavior").glob("trainer_state*.json"))
    if trainer_state_files:
        input_trainer_state_path = trainer_state_files[0]
    else:
        raise FileNotFoundError("Trainer state file not found.")
    trainer_state = TrainerState.model_validate_json(input_trainer_state_path.read_text(encoding="utf-8"))

    launcher.register_session(session_model, rig_model.data_directory)
    store = _behavior_store(session_model)

    suggestion_path: Path | None = None
    curriculum_settings: CurriculumSettings | None = None

    if ui.prompt_confirm(
        ui.ConfirmRequest(
            label="Would you like to run curriculum evaluation and metadata mapping?",
            default=True,
        )
    ):
        (
            _,
            suggestion_path,
            curriculum_settings,
        ) = await run_curriculum_if_applicable(store, trainer_state, input_trainer_state_path, launcher)

        session_end_time: datetime.datetime | None = None
        while session_end_time is None:
            try:
                s = launcher.frontend.prompt_text(
                    ui.TextRequest(
                        label="Enter the session end time in ISO format (YYYY-MM-DDTHH:MM:SSz), e.g: 2024-01-01T12:00:00Z:"
                    )
                )
                session_end_time = datetime.datetime.fromisoformat(s)
            except ValueError:
                logger.error("Invalid date format. Please enter the date in ISO format.")
                ui.notify(
                    "Invalid date format. Please use ISO format (YYYY-MM-DDTHH:MM:SSz).",
                    ui.MessageLevel.WARNING,
                )

        run_vr_foraging_mappers(launcher, suggestion_path, curriculum_settings, session_end_time)
        run_fip_mapper(launcher)

        ui.notify("Data mapping complete.", ui.MessageLevel.SUCCESS)
    else:
        ui.notify(
            "Curriculum evaluation and metadata mapping skipped.",
            ui.MessageLevel.WARNING,
        )

    run_data_qc(launcher)
    run_data_transfer(launcher, session_model)
