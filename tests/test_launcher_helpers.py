import datetime
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from clabe import ui

from experiments import launcher_helpers
from experiments.launcher_helpers import get_session_duration, run_data_transfer

START = datetime.datetime(2026, 11, 5, 12, 0, tzinfo=datetime.UTC)
MIN_DURATION = datetime.timedelta(minutes=5)


def _write_events(session_directory: Path, name: str, events: list[dict[str, Any]]) -> None:
    path = session_directory / "behavior" / "SoftwareEvents" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")


def _write_bounds(session_directory: Path, duration: datetime.timedelta) -> None:
    _write_events(session_directory, "StartSessionTime", [{"data": START.isoformat(), "timestamp": 0}])
    _write_events(session_directory, "EndSessionTime", [{"data": (START + duration).isoformat(), "timestamp": 0}])


class TestSessionDuration:
    def test_from_start_and_end_events(self, tmp_path):
        _write_bounds(tmp_path, datetime.timedelta(minutes=42))
        assert get_session_duration(tmp_path, START) == datetime.timedelta(minutes=42)

    def test_missing_start_falls_back_to_the_session_date(self, tmp_path):
        end = START + datetime.timedelta(minutes=7)
        _write_events(tmp_path, "EndSessionTime", [{"data": end.isoformat(), "timestamp": 0}])
        assert get_session_duration(tmp_path, START) == datetime.timedelta(minutes=7)

    def test_missing_end_falls_back_to_active_site_span(self, tmp_path):
        _write_events(tmp_path, "StartSessionTime", [{"data": START.isoformat(), "timestamp": 0}])
        _write_events(tmp_path, "ActiveSite", [{"timestamp": t} for t in (100.0, 160.0, 280.0)])
        assert get_session_duration(tmp_path, START) == datetime.timedelta(seconds=180)

    def test_unknown_without_events(self, tmp_path):
        assert get_session_duration(tmp_path, START) is None

    def test_unknown_with_corrupt_events(self, tmp_path):
        _write_events(tmp_path, "EndSessionTime", [{"data": "not a date"}])
        assert get_session_duration(tmp_path, START) is None


class FakeTransferService:
    instances: ClassVar[list["FakeTransferService"]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.transferred = False
        FakeTransferService.instances.append(self)

    def add_data_description_tags(self, *tags: str) -> None:
        pass

    def transfer(self) -> None:
        self.transferred = True


class FakeRobocopy:
    calls: ClassVar[int] = 0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def transfer(self) -> None:
        FakeRobocopy.calls += 1


class TestRunDataTransfer:
    @pytest.fixture(autouse=True)
    def _fakes(self, monkeypatch: pytest.MonkeyPatch):
        FakeTransferService.instances = []
        FakeRobocopy.calls = 0
        monkeypatch.setattr(launcher_helpers, "WatchdogDataTransferService", FakeTransferService)
        monkeypatch.setattr(launcher_helpers, "RobocopyService", FakeRobocopy)
        monkeypatch.setattr(launcher_helpers, "WatchdogSettings", lambda **_: SimpleNamespace(destination="dest"))
        self.prompts: list[str] = []
        self.answers: dict[str, bool] = {}

        def fake_prompt(request: ui.ConfirmRequest) -> bool:
            self.prompts.append(request.label)
            return next((v for k, v in self.answers.items() if k in request.label), True)

        monkeypatch.setattr(ui, "prompt_confirm", fake_prompt)

    def _run(self, tmp_path: Path, min_duration: datetime.timedelta | None = MIN_DURATION) -> bool:
        launcher = SimpleNamespace(session_directory=tmp_path / "session")
        launcher.session_directory.mkdir(exist_ok=True)
        session = SimpleNamespace(subject="828424", date=START)
        run_data_transfer(launcher, session, min_session_duration=min_duration)  # type: ignore[arg-type]
        return FakeTransferService.instances[0].transferred

    def test_long_session_is_submitted(self, tmp_path):
        _write_bounds(tmp_path / "session", datetime.timedelta(minutes=30))
        assert self._run(tmp_path) is True
        assert FakeRobocopy.calls == 1

    def test_short_session_is_robocopied_but_not_submitted(self, tmp_path):
        _write_bounds(tmp_path / "session", datetime.timedelta(minutes=4, seconds=59))
        assert self._run(tmp_path) is False
        assert FakeRobocopy.calls == 1

    def test_unknown_duration_asks_and_respects_no(self, tmp_path):
        self.answers["Could not determine the session duration"] = False
        assert self._run(tmp_path) is False
        assert any("Could not determine the session duration" in p for p in self.prompts)
        assert FakeRobocopy.calls == 1

    def test_unknown_duration_asks_and_respects_yes(self, tmp_path):
        self.answers["Could not determine the session duration"] = True
        assert self._run(tmp_path) is True

    def test_no_minimum_skips_the_check(self, tmp_path):
        _write_bounds(tmp_path / "session", datetime.timedelta(seconds=10))
        assert self._run(tmp_path, min_duration=None) is True
