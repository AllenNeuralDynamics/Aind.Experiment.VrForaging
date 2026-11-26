"""Steps and helpers shared by the experiments: logs, recovery prompts and the data transfer."""

import datetime
import json
import logging
from pathlib import Path
from typing import Any

from aind_behavior_services.session import Session
from clabe import ui
from clabe.aind.smartsheet import SmartsheetScheduleClient
from clabe.aind.watchdog import WatchdogDataTransferService, WatchdogSettings
from clabe.data_transfer.robocopy import RobocopyService, RobocopySettings
from clabe.launcher import Launcher
from clabe.logging import otel
from clabe.workflow import step_defaults

logger = logging.getLogger(__name__)

# Seconds the launcher waits at its "Press Enter to exit" prompt before closing on its own.
EXIT_PROMPT_TIMEOUT_S = 60


@step_defaults(title="Copy logs")
def copy_logs(launcher: Launcher) -> None:
    """Copies the launcher's logs to the session directory."""
    launcher.copy_logs()


def _iso_datetime_error(value: str) -> str | None:
    try:
        datetime.datetime.fromisoformat(value)
    except ValueError:
        return "Invalid date format. Please use ISO format (YYYY-MM-DDTHH:MM:SSz)."
    return None


def ask_session_end_time() -> datetime.datetime:
    """Asks for the end time of a session being recovered."""
    answer = ui.text(
        "Enter the session end time in ISO format (YYYY-MM-DDTHH:MM:SSz), e.g: 2024-01-01T12:00:00Z:",
        validators=[_iso_datetime_error],
    )
    return datetime.datetime.fromisoformat(answer)


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
        return ui.confirm("Could not determine the session duration. Submit the session for upload anyway?")
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

    The sources are ``launcher.session_directories``, this computer's session folders.

    With `min_session_duration`, sessions shorter than it (or of unknown duration, unless the operator
    confirms) are still robocopied off the rig but no watchdog manifest is created, so they are not uploaded.
    """
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
            if not ui.confirm("Continue the data transfer with the default project name?"):
                ui.notify("Data transfer skipped.", ui.MessageLevel.WARNING)
                return
    watchdog_settings.destination = Path(watchdog_settings.destination) / session.subject

    transfer_service = WatchdogDataTransferService(
        source=launcher.session_directories,
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
