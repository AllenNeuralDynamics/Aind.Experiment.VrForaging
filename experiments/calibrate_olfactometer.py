"""Olfactometer calibration experiment."""

from pathlib import Path

from aind_behavior_device_olfactometer.rig import OlfactometerCalibrationRig
from aind_behavior_device_olfactometer.task_logic import (
    OlfactometerCalibrationLogic,
    OlfactometerCalibrationParameters,
)
from aind_behavior_services.session import Session
from aind_behavior_services.utils import utcnow
from clabe import resource_monitor, ui
from clabe.apps import AindBehaviorServicesBonsaiApp
from clabe.launcher import Launcher, experiment
from clabe.session import SessionBuilder
from clabe.stores import Kind, LocalFileStore

_OLFACTOMETER_CONFIG_LIBRARY = Path(r"\\allen\aind\scratch\AindBehavior.db\AindBehaviorDeviceOlfactometer")
_OLFACTOMETER_RIG = Kind.from_rig(OlfactometerCalibrationRig)
_CALIBRATION_PARAMETER_FIELDS = ("full_flow_rate", "n_repeats_per_stimulus", "time_on", "time_off")


def _calibration_session(launcher: Launcher) -> Session:
    experimenter = SessionBuilder(launcher).prompt_experimenter()
    return Session(
        subject="CALIBRATION",
        experiment="OlfactometerCalibration",
        date=utcnow(),
        experimenter=experimenter,
        notes="Session for OlfactometerCalibration. No animal is being run.",
    )


@experiment(name="calibrate-olfactometer", order=3)
async def calibrate_olfactometer(launcher: Launcher) -> None:
    """Calibrate the rig's olfactometer hardware."""
    rig = LocalFileStore(_OLFACTOMETER_CONFIG_LIBRARY).resolve(_OLFACTOMETER_RIG)

    resource_monitor.ResourceMonitor(
        constrains=[resource_monitor.available_storage_constraint_factory(rig.data_directory, 2e10)]
    ).run()

    task_parameters = OlfactometerCalibrationParameters(
        **{
            field_name: ui.prompt_field(ui.FieldRequest(model=OlfactometerCalibrationParameters, field_name=field_name))
            for field_name in _CALIBRATION_PARAMETER_FIELDS
        }
    )
    task = OlfactometerCalibrationLogic(task_parameters=task_parameters)
    session = _calibration_session(launcher)
    launcher.register_session(session, rig.data_directory)

    await AindBehaviorServicesBonsaiApp(
        workflow=Path(r"./Aind.Behavior.Device.Olfactometer/src/main.bonsai"),
        executable=Path(r"./Aind.Behavior.Device.Olfactometer/.bonsai/Bonsai.exe"),
        temp_directory=launcher.temp_dir,
        rig=rig,
        session=session,
        task=task,
    ).run_async()
    ui.notify("Olfactometer calibration completed successfully.", ui.MessageLevel.SUCCESS)
