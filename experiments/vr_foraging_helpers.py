"""VrForaging helpers: Smartsheet lookups, the manipulator position, the mappers and the QC report."""

import datetime
import logging
from pathlib import Path
from typing import Any, cast

from aind_behavior_services.rig.aind_manipulator import ManipulatorPosition
from aind_behavior_services.session import Session
from aind_behavior_vr_foraging import data_contract
from aind_behavior_vr_foraging.rig import AindVrForagingRig
from aind_behavior_vr_foraging_data_mappers import DataMapperCli
from clabe import ui
from clabe.aind.modifiers import ByAnimalModifier
from clabe.aind.smartsheet import SmartsheetScheduleClient
from clabe.apps import CurriculumOutcome
from clabe.launcher import Launcher
from clabe.logging import otel
from clabe.stores import Kind, Store
from contraqctor.contract.json import SoftwareEvents

logger = logging.getLogger(__name__)

_MANIPULATOR_POSITION = Kind(ManipulatorPosition, name="manipulator_init")


def add_scientific_contact_or_warn(smartsheet_client: SmartsheetScheduleClient, session: Session) -> Session:
    """Adds the scientific contact to the session, or warns and returns it unchanged (e.g. test runs)."""
    try:
        return smartsheet_client.add_scientific_contact(session)
    except ValueError as e:
        logger.warning("Could not add scientific contact from Smartsheet: %s", e)
        ui.notify(f"Continuing without a scientific contact: {e}", ui.MessageLevel.WARNING)
        return session


def disable_sniff_detector_without_thermistor(
    smartsheet_client: SmartsheetScheduleClient, session: Session, rig: AindVrForagingRig
) -> None:
    """Removes the harp sniff detector from the rig unless the smartsheet says the animal has a thermistor.

    The smartsheet stores ``has_thermistor`` as the string "true" or "false". A missing row,
    column or value is treated as no thermistor.
    """
    row = smartsheet_client.get_row(session.subject)
    value = getattr(row, "has_thermistor", None)
    if str(value).strip().lower() != "true":
        logger.info("Subject '%s' has no confirmed thermistor; disabling harp_sniff_detector.", session.subject)
        rig.harp_sniff_detector = None


def run_vr_foraging_mappers(
    launcher: Launcher,
    curriculum: CurriculumOutcome | None,
    session_end_time: datetime.datetime,
) -> None:
    """Maps the VrForaging data to the data schema (``acquisition.json``, ``instrument.json``, ...)."""
    assert launcher.repository.working_tree_dir is not None
    ui.notify("Running data mappers...", ui.MessageLevel.INFO)
    DataMapperCli(
        data_path=launcher.session_directory,
        repository_path=Path(launcher.repository.working_tree_dir) / "Aind.Behavior.VrForaging",
        curriculum_suggestion=curriculum.suggestion_path if curriculum else None,
        curriculum_repository_path=curriculum.settings.project_directory if curriculum else None,
        session_end_time=session_end_time,
    ).cli_cmd()


def run_data_qc(launcher: Launcher) -> None:
    """Generates the VrForaging QC report and opens it. Failures are reported, not raised."""
    try:
        import webbrowser

        from aind_behavior_vr_foraging.data_qc.data_qc import make_qc_runner
        from contraqctor.qc.reporters import HtmlReporter

        ui.notify("Running data QC...", ui.MessageLevel.INFO)
        vr_dataset = data_contract.dataset(launcher.session_directory)
        runner = make_qc_runner(vr_dataset)
        qc_path = launcher.session_directory / "Behavior" / "Logs" / "qc_report.html"
        reporter = HtmlReporter(output_path=qc_path)
        runner.run_all_with_progress(reporter=reporter)
        ui.notify(f"QC report saved to {qc_path}", ui.MessageLevel.SUCCESS)
        webbrowser.open(qc_path.as_uri(), new=2)
    except Exception as e:  # noqa: BLE001 -- QC failures should be reported without aborting the session
        logger.error("Failed to run data QC: %s", e)
        ui.notify(f"Failed to run data QC: {e}", ui.MessageLevel.ERROR)
        otel.record_exception(e)


class ByAnimalManipulatorModifier(ByAnimalModifier[AindVrForagingRig]):
    """Modifier to set and update manipulator initial position based on animal-specific data."""

    def __init__(
        self,
        subject: str,
        store: Store,
        *,
        launcher: Launcher,
    ) -> None:
        super().__init__(
            subject,
            store,
            _MANIPULATOR_POSITION,
            "manipulator.calibration.initial_position",
        )
        self._launcher = launcher

    def _process_before_update(self) -> ManipulatorPosition:
        _dataset = data_contract.dataset(self._launcher.session_directory)
        manipulator_parking_position: SoftwareEvents = cast(
            SoftwareEvents,
            _dataset["Behavior"]["SoftwareEvents"]["SpoutParkingPositions"].load(),
        )
        data: dict[str, Any] = manipulator_parking_position.data.iloc[-1]["data"]["ResetPosition"]
        return ManipulatorPosition.model_validate(data)
