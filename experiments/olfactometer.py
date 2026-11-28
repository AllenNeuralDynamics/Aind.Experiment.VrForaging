"""Olfactometer calibration, and its recovery after a launcher crash."""

import datetime
from pathlib import Path

from aind_behavior_device_olfactometer.data_mappers import map_dataset
from aind_behavior_device_olfactometer.rig import OlfactometerCalibrationRig
from aind_behavior_device_olfactometer.task_logic import (
    OlfactometerCalibrationLogic,
    OlfactometerCalibrationParameters,
)
from aind_behavior_services.session import Session
from aind_behavior_services.utils import utcnow
from clabe import checks, steps, ui
from clabe.aind.watchdog import UploadGroup, WatchdogDataTransferService, WatchdogSettings
from clabe.apps import AindBehaviorServicesBonsaiApp, CommandResult
from clabe.launcher import Launcher
from clabe.session import SessionBuilder
from clabe.stores import Kind, LocalFileStore
from clabe.workflow import Abort, Workflow
from pydantic import create_model

from .common import EXIT_PROMPT_TIMEOUT_S, ask_session_end_time, copy_logs

#: AIND marks calibration data assets with this subject id.
#: https://docs.allenneuraldynamics.org/en/latest/acquire_upload/calibration.html
_CALIBRATION_SUBJECT_ID = "calibration"

_REPOSITORY = Path("Aind.Behavior.Device.Olfactometer")
_CONFIG_LIBRARY = Path(r"\\allen\aind\scratch\AindBehavior.db\AindBehaviorDeviceOlfactometer")
_RIG = Kind.from_rig(OlfactometerCalibrationRig)
_PARAMETER_FIELDS = ("full_flow_rate", "n_repeats_per_stimulus", "time_on", "time_off")
_ParametersForm = create_model(
    "OlfactometerCalibrationForm",
    **{
        name: (
            OlfactometerCalibrationParameters.model_fields[name].annotation,
            OlfactometerCalibrationParameters.model_fields[name],
        )
        for name in _PARAMETER_FIELDS
    },
)

calibrate_olfactometer = Workflow(
    "calibrate-olfactometer",
    description="Launches the olfactometer calibration",
    order=3,
    exit_prompt_timeout=EXIT_PROMPT_TIMEOUT_S,
)


@calibrate_olfactometer.step
def session(launcher: Launcher) -> Session:
    """Who calibrates; the subject is AIND's calibration subject, which the data mapper writes to the metadata."""
    return Session(
        subject=_CALIBRATION_SUBJECT_ID,
        experiment=OlfactometerCalibrationLogic.model_fields["name"].default,
        date=utcnow(),
        experimenter=SessionBuilder(launcher).prompt_experimenter(),
    )


@calibrate_olfactometer.step(title="Olfactometer rig")
def rig() -> OlfactometerCalibrationRig:
    return LocalFileStore(_CONFIG_LIBRARY).resolve(_RIG)


calibrate_olfactometer.step(steps.preflight(checks.free_space(2e10)))


@calibrate_olfactometer.step(title="Calibration parameters")
def task() -> OlfactometerCalibrationLogic:
    form = ui.form(_ParametersForm)
    if form is None:
        raise Abort("Calibration cancelled.")
    return OlfactometerCalibrationLogic(
        task_parameters=OlfactometerCalibrationParameters.model_validate(form.model_dump())
    )


calibrate_olfactometer.step(steps.register())


@calibrate_olfactometer.step(cancellable=True)
async def calibrate(
    launcher: Launcher, session: Session, rig: OlfactometerCalibrationRig, task: OlfactometerCalibrationLogic
) -> CommandResult:
    return await AindBehaviorServicesBonsaiApp(
        workflow=_REPOSITORY / "src/main.bonsai",
        executable=_REPOSITORY / ".bonsai/Bonsai.exe",
        temp_directory=launcher.temp_dir,
        rig=rig,
        session=session,
        task=task,
    ).run_async()


@calibrate_olfactometer.step
def session_end_time() -> datetime.datetime:
    """The calibration workflow logs no wall-clock end time, so the mapper needs this one."""
    return utcnow()


@calibrate_olfactometer.step
def mappers(launcher: Launcher, session_end_time: datetime.datetime) -> None:
    """Maps the calibration session to aind-data-schema metadata."""
    assert launcher.repository.working_tree_dir is not None
    ui.notify("Running data mappers...", ui.MessageLevel.INFO)
    map_dataset(
        launcher.session_directory,
        Path(launcher.repository.working_tree_dir) / _REPOSITORY,
        session_end_time=session_end_time,
    ).write_standard_files(launcher.session_directory)


calibrate_olfactometer.step(copy_logs)


@calibrate_olfactometer.step(optional="Would you like to transfer data?")
def transfer(launcher: Launcher, session: Session) -> None:
    """Hands the session to the watchdog, uploaded at once. Unlike VrForaging there is no robocopy first: a
    calibration session is small enough for the watchdog to move."""
    # Its own upload group: clabe.yml's settings are VrForaging's.
    group = UploadGroup(
        name="calibration",
        job_type="upload_only_v2",
        creates_record=True,
        schedule_time=None,
        delete_modalities_source_after_success=True,
    )
    settings = WatchdogSettings(upload_groups=[group])
    settings.destination = Path(settings.destination) / session.subject
    WatchdogDataTransferService(source=launcher.session_directory, settings=settings, session=session).transfer()


# Recovery: map and transfer a calibration whose Bonsai workflow already completed ----------------------------------

recover_olfactometer_calibration = Workflow(
    "recover-olfactometer-calibration",
    description="Recovers an olfactometer calibration that already ran: maps and transfers its data",
    order=5,
    exit_prompt_timeout=EXIT_PROMPT_TIMEOUT_S,
)


@recover_olfactometer_calibration.step
def session_directory() -> Path:
    path = ui.path("Choose the calibration session directory to recover:", kind="dir", field="session_directory")
    if path is None:
        raise Abort("Session recovery cancelled.")
    return path


@recover_olfactometer_calibration.step(name="session")
def recorded_session(session_directory: Path) -> Session:
    return Session.model_validate_json((session_directory / "Behavior/Logs/session_input.json").read_text("utf-8"))


@recover_olfactometer_calibration.step(name="rig", title="Olfactometer rig")
def recorded_rig(session_directory: Path) -> OlfactometerCalibrationRig:
    path = session_directory / "Behavior/Logs/rig_input.json"
    return OlfactometerCalibrationRig.model_validate_json(path.read_text("utf-8"))


recover_olfactometer_calibration.step(steps.register())
recover_olfactometer_calibration.step(ask_session_end_time, name="session_end_time")
recover_olfactometer_calibration.step(mappers)
recover_olfactometer_calibration.step(copy_logs)
recover_olfactometer_calibration.step(transfer, optional="Would you like to transfer data?")
