"""Olfactometer calibration experiment."""

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
from clabe import resource_monitor, ui
from clabe.apps import AindBehaviorServicesBonsaiApp
from clabe.data_transfer.aind_watchdog import (
    WatchdogDataTransferService,
    WatchdogSettings,
)
from clabe.launcher import Launcher, experiment
from clabe.session import SessionBuilder
from clabe.stores import Kind, LocalFileStore
from pydantic import create_model

#: AIND marks calibration data assets with this subject id.
#: https://docs.allenneuraldynamics.org/en/latest/acquire_upload/calibration.html
_CALIBRATION_SUBJECT_ID = "calibration"

_OLFACTOMETER_REPOSITORY = "Aind.Behavior.Device.Olfactometer"
_OLFACTOMETER_CONFIG_LIBRARY = Path(r"\\allen\aind\scratch\AindBehavior.db\AindBehaviorDeviceOlfactometer")
_OLFACTOMETER_RIG = Kind.from_rig(OlfactometerCalibrationRig)
_CALIBRATION_PARAMETER_FIELDS = ("full_flow_rate", "n_repeats_per_stimulus", "time_on", "time_off")
_OlfactometerCalibrationForm = create_model(
    "OlfactometerCalibrationForm",
    **{
        field_name: (
            OlfactometerCalibrationParameters.model_fields[field_name].annotation,
            OlfactometerCalibrationParameters.model_fields[field_name],
        )
        for field_name in _CALIBRATION_PARAMETER_FIELDS
    },
)


def _calibration_session(launcher: Launcher) -> Session:
    experimenter = SessionBuilder(launcher).prompt_experimenter()
    return Session(
        # AIND marks calibration assets with this subject id, and it is what the data
        # mapper writes into the acquisition and subject metadata.
        subject=_CALIBRATION_SUBJECT_ID,
        experiment=OlfactometerCalibrationLogic.model_fields["name"].default,
        date=utcnow(),
        experimenter=experimenter,
    )


def _run_mappers(launcher: Launcher, session_end_time: datetime.datetime) -> None:
    """Map the calibration session to aind-data-schema metadata."""
    assert launcher.repository.working_tree_dir is not None
    repository_root = Path(launcher.repository.working_tree_dir)
    # Standalone Olfactometer checkouts have the package at the repo root; when this repo
    # composes it as a submodule it lives one level deeper.
    olfactometer_repository_path = (
        repository_root / _OLFACTOMETER_REPOSITORY
        if (repository_root / _OLFACTOMETER_REPOSITORY).exists()
        else repository_root
    )

    ui.notify("Running data mappers...", ui.MessageLevel.INFO)
    mapped = map_dataset(
        launcher.session_directory,
        olfactometer_repository_path,
        session_end_time=session_end_time,
    )
    mapped.write_standard_files(launcher.session_directory)


def _run_data_transfer(launcher: Launcher, session: Session) -> None:
    """Hand the calibration session to the watchdog.

    Unlike the VR Foraging sessions there is no upfront robocopy: a calibration session is
    small enough that letting the watchdog move it is sufficient.
    """
    if not ui.prompt_confirm(ui.ConfirmRequest(label="Would you like to transfer data?", default=True)):
        ui.notify("Data transfer skipped.", ui.MessageLevel.WARNING)
        return

    watchdog_settings = WatchdogSettings()
    watchdog_settings.destination = Path(watchdog_settings.destination) / session.subject
    watchdog_settings.job_type = "upload_only_v2"
    WatchdogDataTransferService(
        source=launcher.session_directory,
        settings=watchdog_settings,
        session=session,
    ).transfer()


@experiment(name="calibrate-olfactometer", order=3)
async def calibrate_olfactometer(launcher: Launcher) -> None:
    """Calibrate the rig's olfactometer hardware."""
    session = _calibration_session(launcher)

    rig = LocalFileStore(_OLFACTOMETER_CONFIG_LIBRARY).resolve(_OLFACTOMETER_RIG)

    resource_monitor.ResourceMonitor(
        constrains=[resource_monitor.available_storage_constraint_factory(rig.data_directory, 2e10)]
    ).run()

    form_values = ui.prompt_form(ui.FormRequest(model=_OlfactometerCalibrationForm))
    if form_values is None:
        return
    task_parameters = OlfactometerCalibrationParameters.model_validate(form_values.model_dump())
    task = OlfactometerCalibrationLogic(task_parameters=task_parameters)
    launcher.register_session(session, rig.data_directory)

    await AindBehaviorServicesBonsaiApp(
        workflow=Path(r"./Aind.Behavior.Device.Olfactometer/src/main.bonsai"),
        executable=Path(r"./Aind.Behavior.Device.Olfactometer/.bonsai/Bonsai.exe"),
        temp_directory=launcher.temp_dir,
        rig=rig,
        session=session,
        task=task,
    ).run_async()
    # The workflow logs no wall-clock end time, so the mapper needs ours.
    session_end_time = utcnow()
    ui.notify("Olfactometer calibration completed successfully.", ui.MessageLevel.SUCCESS)

    # Mappers
    _run_mappers(launcher, session_end_time)
    ui.notify("Data mapping complete.", ui.MessageLevel.SUCCESS)

    # Watchdog
    launcher.copy_logs()
    _run_data_transfer(launcher, session)
