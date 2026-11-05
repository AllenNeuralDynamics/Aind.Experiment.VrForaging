"""Curriculum helpers: evaluating the curriculum after a session, and the session history it may depend on.

History-dependent curricula read the subject's past sessions from
``<session>/behavior/Logs/past_metrics.jsonl``. The curriculum metrics of every session are mapped into DocDB
under ``acquisition.stimulus_epochs[].performance_metrics.output_parameters``; the most recent ones are fetched
and written there, oldest first, one JSON object per line. The records are passed through untouched: deciding
which of them are usable is up to each curriculum.

Fetching history must never stop a session. Any failure is reported and results in an empty file, which
curricula treat as "no history".
"""

import datetime
import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import requests
from aind_behavior_curriculum import TrainerState
from aind_behavior_services.session import Session
from aind_data_access_api.document_db import MetadataDbClient
from clabe import ui
from clabe.apps import CurriculumApp, CurriculumSettings, CurriculumSuggestion
from clabe.launcher import Launcher
from clabe.logging import otel
from clabe.runnable import runnable
from clabe.stores import Kind, Store

logger = logging.getLogger(__name__)

_TRAINER_STATE = Kind.from_trainer_state()

PAST_METRICS_RELATIVE_PATH = Path("behavior") / "Logs" / "past_metrics.jsonl"

DEFAULT_WINDOW = datetime.timedelta(days=30)
DEFAULT_LIMIT = 5

_DOCDB_HOST = "api.allenneuraldynamics.org"

_REQUEST_TIMEOUT_S = 5
_RETRY_BACKOFF_S = (0.1, 0.25, 1.0, 2.5)
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

# Start times are stored as ISO strings in mixed formats (with or without fractional seconds, possibly with
# offsets), so string comparisons are not exact. The query uses a padded window and the exact bounds are
# applied after parsing.
_QUERY_PADDING = datetime.timedelta(days=1)


class _TimeoutSession(requests.Session):
    """`MetadataDbClient` exposes no timeout, so a hung connection would otherwise stall the launcher."""

    def request(self, *args: Any, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", _REQUEST_TIMEOUT_S)
        return super().request(*args, **kwargs)


def _make_client() -> MetadataDbClient:
    return MetadataDbClient(
        host=_DOCDB_HOST,
        session=_TimeoutSession(),
        version="v2",
    )


def _is_retryable(error: Exception) -> bool:
    if isinstance(error, (requests.ConnectionError, requests.Timeout)):
        return True
    if isinstance(error, requests.HTTPError) and error.response is not None:
        return error.response.status_code in _RETRYABLE_STATUS_CODES
    return False


def _call_with_retries[T](fn: Callable[[], T], *, sleep: Callable[[float], None] = time.sleep) -> T:
    """Calls `fn`, retrying transient connection errors with backoff. Other errors are raised immediately."""
    n_attempts = len(_RETRY_BACKOFF_S) + 1
    for attempt in range(1, n_attempts + 1):
        try:
            return fn()
        except Exception as e:
            if not _is_retryable(e) or attempt == n_attempts:
                raise
            delay = _RETRY_BACKOFF_S[attempt - 1]
            logger.warning(
                "DocDB request attempt %d/%d failed (%s: %s). Retrying in %d ms.",
                attempt,
                n_attempts,
                type(e).__name__,
                e,
                round(delay * 1000),
            )
            otel.event(
                "docdb-request-retry",
                {"attempt": attempt, "max_attempts": n_attempts, "error": type(e).__name__, "delay_s": delay},
            )
            sleep(delay)
    raise AssertionError("unreachable")


def _to_utc(value: datetime.datetime) -> datetime.datetime:
    # Naive datetimes are assumed to be UTC, which is what the launcher and the mappers emit.
    return value.replace(tzinfo=datetime.UTC) if value.tzinfo is None else value.astimezone(datetime.UTC)


def _iso(value: datetime.datetime) -> str:
    return _to_utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_start_time(record: dict[str, Any]) -> datetime.datetime | None:
    value = (record.get("acquisition") or {}).get("acquisition_start_time")
    if not isinstance(value, str):
        return None
    try:
        return _to_utc(datetime.datetime.fromisoformat(value))
    except ValueError:
        return None


def _extract_metrics(record: dict[str, Any], task_name: str) -> dict[str, Any] | None:
    for epoch in (record.get("acquisition") or {}).get("stimulus_epochs") or []:
        if epoch.get("stimulus_name") != task_name:
            continue
        metrics = (epoch.get("performance_metrics") or {}).get("output_parameters")
        if metrics:
            return metrics
    return None


def _select_latest(
    records: list[dict[str, Any]],
    *,
    after: datetime.datetime,
    before: datetime.datetime,
    task_name: str,
    limit: int,
) -> list[dict[str, Any]]:
    """Keeps records that started in [after, before) and carry metrics, dedupes them by asset name,
    and returns the metrics of the `limit` most recent, oldest first."""
    by_name: dict[str, tuple[datetime.datetime, dict[str, Any]]] = {}
    for record in records:
        start = _parse_start_time(record)
        if start is None or not (after <= start < before):
            continue
        metrics = _extract_metrics(record, task_name)
        if metrics is None:
            continue
        by_name.setdefault(str(record.get("name") or record.get("_id")), (start, metrics))
    ordered = sorted(by_name.values(), key=lambda item: item[0])
    return [metrics for _, metrics in ordered[-limit:]] if limit > 0 else []


@runnable(
    name="Fetch session history from DocDB",
    # Failures are tolerated and reported by `prepare_past_metrics`, and success is reported with the count.
    notify_success=False,
    notify_fail=False,
)
def fetch_past_metrics(
    subject: str,
    *,
    before: datetime.datetime,
    task_name: str,
    window: datetime.timedelta = DEFAULT_WINDOW,
    limit: int = DEFAULT_LIMIT,
    client_factory: Callable[[], MetadataDbClient] = _make_client,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict[str, Any]]:
    """Returns the curriculum metrics of the `limit` most recent raw `task_name` sessions of `subject`
    that started in [before - window, before), oldest first.

    `before` should be the start time of the session being evaluated, so that sessions run after it
    (e.g. when recovering a session the next day) are never included.

    Runs as a clabe runnable: it shows an activity spinner while running and records an OpenTelemetry span,
    including any exception that escapes it.

    Raises on failure, after retrying transient connection errors.
    """
    before = _to_utc(before)
    after = before - window
    client = client_factory()
    records = _call_with_retries(
        lambda: client.retrieve_docdb_records(
            filter_query={
                "subject.subject_id": subject,
                "data_description.data_level": "raw",
                "acquisition.stimulus_epochs.stimulus_name": task_name,
                "acquisition.acquisition_start_time": {
                    "$gte": _iso(after - _QUERY_PADDING),
                    "$lte": _iso(before + _QUERY_PADDING),
                },
            },
            projection={
                "name": 1,
                "acquisition.acquisition_start_time": 1,
                "acquisition.stimulus_epochs.stimulus_name": 1,
                "acquisition.stimulus_epochs.performance_metrics.output_parameters": 1,
            },
        ),
        sleep=sleep,
    )
    return _select_latest(records, after=after, before=before, task_name=task_name, limit=limit)


def write_past_metrics(path: Path, metrics: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        file.writelines(json.dumps(record) + "\n" for record in metrics)


def prepare_past_metrics(
    session_directory: Path,
    *,
    subject: str,
    before: datetime.datetime,
    task_name: str,
    keep_existing: bool = False,
    client_factory: Callable[[], MetadataDbClient] = _make_client,
    sleep: Callable[[float], None] = time.sleep,
) -> Path:
    """Writes the subject's recent session history to the session directory. Never raises.

    With `keep_existing`, a history file already in the session directory is left untouched. Recovery uses
    this so that the curriculum sees exactly the history the original run saw.
    """
    path = Path(session_directory) / PAST_METRICS_RELATIVE_PATH
    if keep_existing and path.exists():
        logger.info("Keeping existing session history at %s.", path)
        ui.notify("Using the session history saved by the original run.", ui.MessageLevel.INFO)
        return path

    metrics: list[dict[str, Any]] = []
    try:
        metrics = fetch_past_metrics(
            subject, before=before, task_name=task_name, client_factory=client_factory, sleep=sleep
        )
        logger.info("Fetched %d past session(s) for subject %s from DocDB.", len(metrics), subject)
        ui.notify(f"Fetched {len(metrics)} past session(s) from DocDB.", ui.MessageLevel.SUCCESS)
    except Exception as e:  # noqa: BLE001 -- missing history must never abort a session; curricula treat it as none
        logger.error("Could not fetch session history from DocDB: %s: %s", type(e).__name__, e)
        ui.notify(
            f"Could not fetch session history from DocDB ({type(e).__name__}). Continuing without it: "
            "curriculum steps that depend on past sessions will not advance this session.",
            ui.MessageLevel.WARNING,
        )
        # The runnable's span already recorded the exception. Recording it on the current span would mark the
        # whole session as failed for an error that is tolerated.
        otel.event("session-history-unavailable", {"error": type(e).__name__})

    try:
        write_past_metrics(path, metrics)
    except Exception as e:  # noqa: BLE001 -- see above; curricula treat a missing file as no history
        logger.error("Could not write session history to %s: %s", path, e)
        ui.notify(f"Could not write session history: {e}", ui.MessageLevel.WARNING)
        otel.record_exception(e)
    return path


def _dump_suggestion(suggestion: CurriculumSuggestion, session_directory: Path) -> Path:
    path = session_directory / "Behavior" / "Logs" / "suggestion.json"
    logger.info("Dumping curriculum suggestion to: %s", path)
    with open(path, "w", encoding="utf-8") as f:
        f.write(suggestion.model_dump_json(indent=2))
    return path


def is_on_curriculum(trainer_state: TrainerState | None) -> bool:
    return trainer_state is not None and trainer_state.is_on_curriculum is True and trainer_state.stage is not None


async def run_curriculum_if_applicable(
    store: Store,
    trainer_state: TrainerState | None,
    input_trainer_state_path: Path,
    launcher: Launcher,
    session: Session,
    *,
    keep_existing_past_metrics: bool = False,
    upload_suggestion: bool = True,
) -> tuple[CurriculumSuggestion | None, Path | None, CurriculumSettings | None]:
    if not is_on_curriculum(trainer_state):
        return None, None, None
    assert trainer_state is not None and trainer_state.stage is not None
    # History-dependent curricula read the past sessions from the session directory.
    prepare_past_metrics(
        launcher.session_directory,
        subject=session.subject,
        before=session.date,
        task_name=trainer_state.stage.task.name,
        keep_existing=keep_existing_past_metrics,
    )
    ui.notify("Running curriculum evaluation...", ui.MessageLevel.INFO)
    settings = CurriculumSettings(
        input_trainer_state=input_trainer_state_path.resolve(),
        data_directory=launcher.session_directory,
    )
    curriculum_app = CurriculumApp(settings=settings)
    await curriculum_app.run_async()
    suggestion = curriculum_app.process_suggestion()
    suggestion_path = _dump_suggestion(suggestion, launcher.session_directory)
    if upload_suggestion:
        store.write(_TRAINER_STATE, suggestion.trainer_state)
    else:
        logger.info("Curriculum suggestion not uploaded; the subject's trainer state is unchanged.")
        ui.notify("Curriculum suggestion was not uploaded.", ui.MessageLevel.WARNING)
    return suggestion, suggestion_path, settings
