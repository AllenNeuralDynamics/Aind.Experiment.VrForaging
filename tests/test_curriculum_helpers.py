import asyncio
import contextlib
import datetime
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests
from clabe.runnable import get_activity_indicator

from experiments import curriculum_helpers
from experiments.curriculum_helpers import (
    PAST_METRICS_RELATIVE_PATH,
    fetch_past_metrics,
    prepare_past_metrics,
    run_curriculum_if_applicable,
)

TASK = "AindVrForaging"
SESSION_START = datetime.datetime(2026, 11, 5, 12, 0, tzinfo=datetime.UTC)


def _record(name: str, start: str, metrics: dict[str, Any] | None, stimulus_name: str = TASK) -> dict[str, Any]:
    return {
        "name": name,
        "acquisition": {
            "acquisition_start_time": start,
            "stimulus_epochs": [
                {
                    "stimulus_name": stimulus_name,
                    "performance_metrics": None if metrics is None else {"output_parameters": metrics},
                }
            ],
        },
    }


def _day(days_before: float, fmt: str = "%Y-%m-%dT%H:%M:%S.%fZ") -> str:
    return (SESSION_START - datetime.timedelta(days=days_before)).strftime(fmt)


class FakeClient:
    def __init__(self, responses: list[Any]):
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def retrieve_docdb_records(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(kwargs)
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _http_error(status: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(f"{status}", response=response)


def _fetch(client: FakeClient, sleeps: list[float] | None = None, **kwargs: Any) -> list[dict[str, Any]]:
    sleeps = sleeps if sleeps is not None else []
    return fetch_past_metrics(
        "828424", before=SESSION_START, task_name=TASK, client_factory=lambda: client, sleep=sleeps.append, **kwargs
    )


class TestQuery:
    def test_filters_by_subject_level_task_and_padded_window(self):
        client = FakeClient([[]])
        _fetch(client)
        query = client.calls[0]["filter_query"]
        assert query["subject.subject_id"] == "828424"
        assert query["data_description.data_level"] == "raw"
        assert query["acquisition.stimulus_epochs.stimulus_name"] == TASK
        assert query["acquisition.acquisition_start_time"] == {
            "$gte": "2026-10-05T12:00:00Z",  # 30 days + 1 day of padding
            "$lte": "2026-11-06T12:00:00Z",  # 1 day of padding past the session start
        }

    def test_projects_only_the_metrics(self):
        client = FakeClient([[]])
        _fetch(client)
        assert "acquisition.stimulus_epochs.performance_metrics.output_parameters" in client.calls[0]["projection"]


class TestSelection:
    def test_returns_latest_five_oldest_first(self):
        records = [_record(f"s{i}", _day(i), {"i": i}) for i in range(1, 9)]
        assert _fetch(FakeClient([records])) == [{"i": i} for i in (5, 4, 3, 2, 1)]

    def test_excludes_sessions_at_or_after_the_session_start(self):
        records = [
            _record("before", _day(1), {"i": "before"}),
            _record("same", _day(0), {"i": "same"}),
            _record("next_day", _day(-1), {"i": "next_day"}),
        ]
        assert _fetch(FakeClient([records])) == [{"i": "before"}]

    def test_excludes_sessions_older_than_the_window(self):
        records = [_record("old", _day(31), {"i": "old"}), _record("recent", _day(29), {"i": "recent"})]
        assert _fetch(FakeClient([records])) == [{"i": "recent"}]

    def test_window_is_configurable(self):
        records = [_record("a", _day(10), {"i": "a"}), _record("b", _day(3), {"i": "b"})]
        assert _fetch(FakeClient([records]), window=datetime.timedelta(days=5)) == [{"i": "b"}]

    def test_handles_mixed_timestamp_formats(self):
        records = [
            _record("no_fraction", _day(2, "%Y-%m-%dT%H:%M:%SZ"), {"i": 2}),
            _record(
                "offset",
                (SESSION_START - datetime.timedelta(days=1))
                .astimezone(datetime.timezone(datetime.timedelta(hours=-8)))
                .isoformat(),
                {"i": 1},
            ),
            _record("garbage", "not a date", {"i": "garbage"}),
        ]
        assert _fetch(FakeClient([records])) == [{"i": 2}, {"i": 1}]

    def test_skips_records_without_metrics_before_applying_the_limit(self):
        records = [_record(f"s{i}", _day(i), {"i": i}) for i in range(2, 7)]
        records.append(_record("empty", _day(1), None))
        assert [m["i"] for m in _fetch(FakeClient([records]))] == [6, 5, 4, 3, 2]

    def test_ignores_epochs_of_other_tasks(self):
        records = [_record("other", _day(1), {"i": "other"}, stimulus_name="SomethingElse")]
        assert _fetch(FakeClient([records])) == []

    def test_dedupes_by_asset_name(self):
        records = [_record("dup", _day(1), {"i": "first"}), _record("dup", _day(1), {"i": "second"})]
        assert _fetch(FakeClient([records])) == [{"i": "first"}]


class TestRetries:
    @pytest.mark.parametrize(
        "error",
        [requests.ConnectionError("down"), requests.Timeout("slow"), _http_error(503), _http_error(429)],
    )
    def test_retries_transient_errors(self, error: Exception):
        sleeps: list[float] = []
        client = FakeClient([error, error, [_record("s", _day(1), {"i": 1})]])
        assert _fetch(client, sleeps) == [{"i": 1}]
        assert sleeps == [0.1, 0.25]

    def test_gives_up_after_five_attempts(self):
        sleeps: list[float] = []
        client = FakeClient([requests.ConnectionError("down")] * 5)
        with pytest.raises(requests.ConnectionError):
            _fetch(client, sleeps)
        assert len(client.calls) == 5
        assert sleeps == [0.1, 0.25, 1.0, 2.5]

    @pytest.mark.parametrize("error", [_http_error(400), _http_error(404), ValueError("bad json")])
    def test_does_not_retry_other_errors(self, error: Exception):
        sleeps: list[float] = []
        client = FakeClient([error])
        with pytest.raises(type(error)):
            _fetch(client, sleeps)
        assert len(client.calls) == 1
        assert sleeps == []

    def test_requests_have_a_timeout(self, monkeypatch: pytest.MonkeyPatch):
        seen: dict[str, Any] = {}

        def fake_request(self, *args: Any, **kwargs: Any) -> None:
            seen.update(kwargs)

        monkeypatch.setattr(requests.Session, "request", fake_request)
        curriculum_helpers._TimeoutSession().get("http://example.invalid")
        assert seen["timeout"] == curriculum_helpers._REQUEST_TIMEOUT_S


class TestRunnable:
    @pytest.fixture
    def activities(self):
        seen: list[str] = []

        class Sink:
            @contextlib.contextmanager
            def activity(self, description: str):
                seen.append(description)
                yield

        indicator = get_activity_indicator()
        indicator.set_sink(Sink())
        yield seen
        indicator.set_sink(None)

    def test_fetch_shows_activity_while_running(self, activities: list[str]):
        _fetch(FakeClient([[]]))
        assert activities == ["Fetch session history from DocDB"]

    def test_retries_happen_inside_one_activity(self, activities: list[str]):
        _fetch(FakeClient([requests.ConnectionError("down"), []]))
        assert activities == ["Fetch session history from DocDB"]

    def test_client_construction_happens_inside_the_activity(self, activities: list[str]):
        def broken_factory():
            raise RuntimeError("no client")

        with pytest.raises(RuntimeError):
            fetch_past_metrics("828424", before=SESSION_START, task_name=TASK, client_factory=broken_factory)
        assert activities == ["Fetch session history from DocDB"]


class TestPrepare:
    @staticmethod
    def _prepare(tmp_path, client: FakeClient, **kwargs: Any):
        return prepare_past_metrics(
            tmp_path,
            subject="828424",
            before=SESSION_START,
            task_name=TASK,
            client_factory=lambda: client,
            sleep=lambda _: None,
            **kwargs,
        )

    def test_writes_one_record_per_line(self, tmp_path):
        records = [_record("a", _day(2), {"i": 2}), _record("b", _day(1), {"i": 1})]
        path = self._prepare(tmp_path, FakeClient([records]))
        assert path == tmp_path / PAST_METRICS_RELATIVE_PATH
        assert [json.loads(line) for line in path.read_text().splitlines()] == [{"i": 2}, {"i": 1}]

    def test_failure_writes_an_empty_file_and_does_not_raise(self, tmp_path):
        path = self._prepare(tmp_path, FakeClient([requests.ConnectionError("down")] * 5))
        assert path.read_text() == ""

    def test_unexpected_error_writes_an_empty_file_and_does_not_raise(self, tmp_path):
        path = self._prepare(tmp_path, FakeClient([RuntimeError("bug")]))
        assert path.read_text() == ""

    def test_client_construction_failure_does_not_raise(self, tmp_path):
        def broken_factory():
            raise RuntimeError("no client")

        path = prepare_past_metrics(
            tmp_path, subject="828424", before=SESSION_START, task_name=TASK, client_factory=broken_factory
        )
        assert path.read_text() == ""

    def test_overwrites_existing_file_by_default(self, tmp_path):
        path = tmp_path / PAST_METRICS_RELATIVE_PATH
        path.parent.mkdir(parents=True)
        path.write_text('{"i": "stale"}\n')
        self._prepare(tmp_path, FakeClient([[]]))
        assert path.read_text() == ""

    def test_keep_existing_leaves_the_file_and_skips_docdb(self, tmp_path):
        path = tmp_path / PAST_METRICS_RELATIVE_PATH
        path.parent.mkdir(parents=True)
        path.write_text('{"i": "original"}\n')
        client = FakeClient([])
        self._prepare(tmp_path, client, keep_existing=True)
        assert path.read_text() == '{"i": "original"}\n'
        assert client.calls == []

    def test_keep_existing_fetches_when_the_file_is_missing(self, tmp_path):
        path = self._prepare(tmp_path, FakeClient([[_record("a", _day(1), {"i": 1})]]), keep_existing=True)
        assert path.read_text() == '{"i": 1}\n'


class TestRunCurriculumUploadSuggestion:
    @pytest.fixture(autouse=True)
    def _fakes(self, monkeypatch: pytest.MonkeyPatch):
        self.past_metrics_calls: list[dict[str, Any]] = []
        suggestion = SimpleNamespace(trainer_state="new-state")

        class FakeCurriculumApp:
            def __init__(self, settings: Any) -> None:
                pass

            async def run_async(self) -> None:
                pass

            def process_suggestion(self) -> Any:
                return suggestion

        monkeypatch.setattr(curriculum_helpers, "CurriculumApp", FakeCurriculumApp)
        monkeypatch.setattr(curriculum_helpers, "CurriculumSettings", lambda **kwargs: SimpleNamespace(**kwargs))
        monkeypatch.setattr(curriculum_helpers, "_dump_suggestion", lambda s, d: d / "suggestion.json")
        monkeypatch.setattr(
            curriculum_helpers, "prepare_past_metrics", lambda *a, **kw: self.past_metrics_calls.append(kw)
        )

    def _run(self, tmp_path: Path, trainer_state: Any, **kwargs: Any) -> list[Any]:
        writes: list[Any] = []
        store = SimpleNamespace(write=lambda kind, value: writes.append(value))
        launcher = SimpleNamespace(session_directory=tmp_path)
        session = SimpleNamespace(subject="828424", date=SESSION_START)
        asyncio.run(
            run_curriculum_if_applicable(store, trainer_state, tmp_path / "ts.json", launcher, session, **kwargs)  # type: ignore[arg-type]
        )
        return writes

    @staticmethod
    def _on_curriculum() -> Any:
        return SimpleNamespace(
            is_on_curriculum=True, stage=SimpleNamespace(task=SimpleNamespace(name="AindVrForaging"))
        )

    def test_uploads_by_default(self, tmp_path):
        assert self._run(tmp_path, self._on_curriculum()) == ["new-state"]

    def test_does_not_upload_when_declined(self, tmp_path):
        assert self._run(tmp_path, self._on_curriculum(), upload_suggestion=False) == []

    def test_prepares_history_bounded_by_session_start(self, tmp_path):
        self._run(tmp_path, self._on_curriculum(), keep_existing_past_metrics=True)
        assert self.past_metrics_calls == [
            {"subject": "828424", "before": SESSION_START, "task_name": "AindVrForaging", "keep_existing": True}
        ]

    def test_skipped_entirely_when_not_on_curriculum(self, tmp_path):
        off = SimpleNamespace(is_on_curriculum=False, stage=None)
        assert self._run(tmp_path, off) == []
        assert self.past_metrics_calls == []
