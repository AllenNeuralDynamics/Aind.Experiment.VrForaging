import datetime
import json
import logging
from pathlib import Path
from typing import Any, cast

from aind_behavior_curriculum import TrainerState
from aind_behavior_services.rig.aind_manipulator import ManipulatorPosition
from aind_behavior_services.session import Session
from aind_behavior_vr_foraging import data_contract
from aind_behavior_vr_foraging.rig import AindVrForagingRig
from aind_behavior_vr_foraging_data_mappers import DataMapperCli
from aind_physiology_fip.data_mappers import ProtoAcquisitionMapper
from clabe import ui
from clabe.apps import CurriculumSettings
from clabe.data_transfer.aind_watchdog import (
    WatchdogDataTransferService,
    WatchdogSettings,
)
from clabe.data_transfer.robocopy import RobocopyService, RobocopySettings
from clabe.launcher import Launcher
from clabe.logging import otel
from clabe.modifiers import ByAnimalModifier
from clabe.stores import Kind, Store
from clabe.utils.aind_smartsheet import SmartsheetScheduleClient
from contraqctor.contract.json import SoftwareEvents

logger = logging.getLogger(__name__)

_MANIPULATOR_POSITION = Kind(ManipulatorPosition, name="manipulator_init")


def confirm_session_info(launcher: Launcher, session: Session, trainer_state: TrainerState) -> bool:
    summary = {
        "Mouse": session.subject,
        "Experimenter": ", ".join(session.experimenter) if session.experimenter else None,
        "Curriculum": trainer_state.curriculum.name if trainer_state and trainer_state.curriculum else None,
        "Stage": trainer_state.stage.name if trainer_state and trainer_state.stage else None,
    }
    return launcher.frontend.prompt_read_only_table(
        ui.ReadOnlyTable.from_object(
            summary,
            title="Confirm Session Information",
            prompt="Is this information correct?",
        )
    )


def run_data_qc(launcher: Launcher) -> None:
    if not ui.prompt_confirm(ui.ConfirmRequest(label="Would you like to generate a qc report?", default=False)):
        return
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

    run_fip_data_qc(launcher)


def run_fip_data_qc(launcher: Launcher) -> None:
    """Run FIP QC when the session contains FIP recordings."""
    fib_dir = launcher.session_directory / "fib"
    if not fib_dir.exists():
        return
    try:
        from aind_physiology_fip.data_qc import DataQcCli

        ui.notify("Running FIP data QC...", ui.MessageLevel.INFO)
        qc_assets_dir = launcher.session_directory / "FIB" / "fip_qc_assets"
        for epoch in sorted(fib_dir.glob("fip_*")):
            DataQcCli(data_path=epoch, asset_path=qc_assets_dir).cli_cmd()
        ui.notify(f"FIP QC assets saved to {qc_assets_dir}", ui.MessageLevel.SUCCESS)
    except Exception as e:  # noqa: BLE001 -- QC failures should be reported without aborting the session
        logger.error("Failed to run FIP data QC: %s", e)
        ui.notify(f"Failed to run FIP data QC: {e}", ui.MessageLevel.ERROR)
        otel.record_exception(e)


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


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def get_session_duration(session_directory: Path, fallback_start_time: datetime.datetime) -> datetime.timedelta | None:
    """Returns the duration of a VrForaging session, or None if it cannot be determined.

    Mirrors how the VrForaging acquisition mapper bounds the session: from the Bonsai ``StartSessionTime``
    and ``EndSessionTime`` events, falling back to `fallback_start_time` for a missing start and to the span
    of the ``ActiveSite`` events for a missing end.
    """
    events = session_directory / "behavior" / "SoftwareEvents"
    try:
        start_events = _read_jsonl(events / "StartSessionTime.json")
        end_events = _read_jsonl(events / "EndSessionTime.json")
        if end_events:
            start = datetime.datetime.fromisoformat(start_events[0]["data"]) if start_events else fallback_start_time
            return datetime.datetime.fromisoformat(end_events[-1]["data"]) - start
        timestamps = [event["timestamp"] for event in _read_jsonl(events / "ActiveSite.json")]
        if timestamps:
            return datetime.timedelta(seconds=max(timestamps) - min(timestamps))
    except (OSError, ValueError, KeyError, TypeError) as e:
        logger.error("Could not determine the session duration: %s: %s", type(e).__name__, e)
    return None


def _session_meets_min_duration(launcher: Launcher, session: Session, min_duration: datetime.timedelta) -> bool:
    duration = get_session_duration(launcher.session_directory, session.date)
    if duration is None:
        logger.warning("Session duration could not be determined.")
        return ui.prompt_confirm(
            ui.ConfirmRequest(
                label="Could not determine the session duration. Submit the session for upload anyway?",
                default=False,
            )
        )
    if duration < min_duration:
        logger.warning("Session lasted %s, less than the minimum of %s.", duration, min_duration)
        ui.notify(
            f"Session lasted {duration}, less than the minimum of {min_duration}. "
            "It will be copied to the network share but not submitted for upload.",
            ui.MessageLevel.WARNING,
        )
        return False
    return True


def run_data_transfer(
    launcher: Launcher,
    session: Session,
    *,
    smartsheet_client: SmartsheetScheduleClient | None = None,
    min_session_duration: datetime.timedelta | None = None,
) -> None:
    """Transfer session data, using Smartsheet metadata when a client is provided.

    With `min_session_duration`, sessions shorter than it (or of unknown duration, unless the operator
    confirms) are still robocopied off the rig but no watchdog manifest is created, so they are not uploaded.
    """
    if not ui.prompt_confirm(ui.ConfirmRequest(label="Would you like to transfer data?", default=True)):
        ui.notify("Data transfer skipped.", ui.MessageLevel.WARNING)
        return

    submit_for_upload = min_session_duration is None or _session_meets_min_duration(
        launcher, session, min_session_duration
    )

    watchdog_settings = WatchdogSettings()
    if smartsheet_client is not None:
        try:
            watchdog_settings = WatchdogSettings(project_name=smartsheet_client.get_project_name(session))
        except ValueError as e:
            logger.warning("Could not get project name from Smartsheet: %s", e)
            ui.notify(f"Could not get the project name from Smartsheet: {e}", ui.MessageLevel.WARNING)
            if not ui.prompt_confirm(
                ui.ConfirmRequest(label="Continue the data transfer with the default project name?", default=False)
            ):
                ui.notify("Data transfer skipped.", ui.MessageLevel.WARNING)
                return
    watchdog_settings.destination = Path(watchdog_settings.destination) / session.subject

    transfer_service = WatchdogDataTransferService(
        source=launcher.session_directory,
        settings=watchdog_settings,
        session=session,
    )
    if smartsheet_client is not None:
        smartsheet_row = smartsheet_client.get_row(session.subject)
        if smartsheet_row is not None:
            transfer_service.add_data_description_tags(*smartsheet_row.tags)

    # Immediate robocopy to move behavior data off the rig before triggering watchdog.
    try:
        RobocopyService(
            source=launcher.session_directory,
            settings=RobocopySettings(
                delete_src=False,
                destination=Path(watchdog_settings.destination) / launcher.session_directory.name,
                exclude_dirs=["behavior-videos", "fib"],
            ),
        ).transfer()
    except Exception as e:  # noqa: BLE001 -- transfer failures are reported and retried by the watchdog
        logger.error("Initial data transfer failed: %s", e)
        ui.notify(f"Initial data transfer failed: {e}", ui.MessageLevel.ERROR)
        otel.record_exception(e)

    if not submit_for_upload:
        logger.warning("Session not submitted for upload; no watchdog manifest was created.")
        ui.notify("Session not submitted for upload.", ui.MessageLevel.WARNING)
        otel.event("session-upload-rejected")
        return
    transfer_service.transfer()


def run_vr_foraging_mappers(
    launcher: Launcher,
    suggestion_path: Path | None,
    curriculum_settings: CurriculumSettings | None,
    session_end_time: datetime.datetime,
) -> None:
    assert launcher.repository.working_tree_dir is not None
    repository_root = Path(launcher.repository.working_tree_dir)
    # Standalone VrForaging checkouts have the package at the repo root; when
    # this repo composes VrForaging as a submodule it lives one level deeper.
    vr_foraging_repository_path = (
        repository_root / "Aind.Behavior.VrForaging"
        if (repository_root / "Aind.Behavior.VrForaging").exists()
        else repository_root
    )

    ui.notify("Running data mappers...", ui.MessageLevel.INFO)
    DataMapperCli(
        data_path=launcher.session_directory,
        repository_path=vr_foraging_repository_path,
        curriculum_suggestion=suggestion_path,
        curriculum_repository_path=curriculum_settings.project_directory if curriculum_settings else None,
        session_end_time=session_end_time,
    ).cli_cmd()


def run_fip_mapper(launcher: Launcher) -> None:
    if not (launcher.session_directory / "fib").exists():
        logger.info(
            "No 'fib' folder found under %s; skipping FIP mapper.",
            launcher.session_directory,
        )
        return
    fip_extracted = ProtoAcquisitionMapper(launcher.session_directory).map()
    (launcher.session_directory / "fip.json").write_text(fip_extracted.model_dump_json(indent=2), encoding="utf-8")


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
