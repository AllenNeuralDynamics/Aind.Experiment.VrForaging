"""The FIP-only workflows (acquisition and photobleaching), and FIP's mapper and QC helpers."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aind_physiology_fip.data_qc
import pytest
from aind_behavior_services.session import Session
from clabe.workflow import RunStatus

from experiments import fip

from ._workflow_runner import FakeLauncher, execute, started_steps

TRANSFER = "Would you like to transfer data?"


class FakeApp:
    def __init__(self, log: list[str], name: str) -> None:
        self.log, self.name = log, name

    async def run_async(self) -> str:
        self.log.append(f"{self.name} runs")
        return self.name


@pytest.fixture
def launcher(tmp_path: Path) -> FakeLauncher:
    return FakeLauncher(session_directory=tmp_path / "session", temp_dir=tmp_path / "temp")


def test_fip_session_acquires_then_maps_qcs_and_copies_logs(monkeypatch, launcher: FakeLauncher):
    log: list[str] = []
    session = Session(subject="test-mouse")
    monkeypatch.setattr(fip, "fip_app", lambda launcher, rig, session: FakeApp(log, "fip"))
    monkeypatch.setattr(fip, "run_mapper", lambda launcher: log.append("mapper"))
    monkeypatch.setattr(fip, "run_data_qc", lambda launcher: log.append("qc"))
    monkeypatch.setattr(fip, "run_data_transfer", lambda launcher, session: log.append(f"transfer {session.subject}"))
    workflow = (
        fip.fip.variant("test-fip")
        .replace("session", lambda: session)
        .replace("rig", lambda: "fip-rig")
        .replace("preflight", lambda: None)
        .replace("register", lambda: None)
    )

    run, client = execute(workflow, {TRANSFER: True}, launcher=launcher)

    assert run.status is RunStatus.COMPLETED
    assert client.asked == [TRANSFER]
    assert log == ["fip runs", "mapper", "qc", "transfer test-mouse"]
    assert started_steps(run).index("copy_logs") < started_steps(run).index("transfer")


def test_photobleaching_runs_the_photobleaching_workflow_with_the_rig_only(monkeypatch, launcher: FakeLauncher):
    started: list[dict[str, Any]] = []

    class FakeBonsaiApp:
        def __init__(self, **kwargs: Any) -> None:
            started.append(kwargs)

        async def run_async(self) -> str:
            return "done"

    monkeypatch.setattr(fip, "AindBehaviorServicesBonsaiApp", FakeBonsaiApp)
    workflow = fip.fip_photobleaching.variant("test-photobleaching").replace("rig", lambda: "fip-rig")

    run, client = execute(workflow, launcher=launcher)

    assert run.status is RunStatus.COMPLETED and client.asked == []
    assert started == [
        {
            "workflow": Path("./Aind.Physiology.Fip/src/photobleaching.bonsai"),
            "executable": Path("./Aind.Physiology.Fip/.bonsai/bonsai.exe"),
            "temp_directory": launcher.temp_dir,
            "rig": "fip-rig",
            "is_editor_mode": False,
        }
    ]
    # Exits on success without waiting for Enter (clabe still waits after a failure).
    assert fip.fip_photobleaching.exit_prompt_timeout == 0


# Mapper and QC ----------------------------------------------------------------------------------------------------


def test_mapper_does_nothing_without_fip_data(monkeypatch, launcher: FakeLauncher):
    monkeypatch.setattr(fip, "FipDataMapperCli", lambda **kwargs: pytest.fail("the mapper must not run"))
    fip.run_mapper(launcher)


class RecordingQc:
    def __init__(self, calls: list[tuple[Path, Path]], *, fail_on: str | None = None) -> None:
        self.calls, self.fail_on = calls, fail_on

    def __call__(self, *, data_path: Path, asset_path: Path) -> SimpleNamespace:
        def cli_cmd() -> None:
            if data_path.name == self.fail_on:
                raise RuntimeError("bad epoch")
            self.calls.append((data_path, asset_path))

        return SimpleNamespace(cli_cmd=cli_cmd)


def test_qc_runs_once_per_epoch_in_order(monkeypatch, launcher: FakeLauncher):
    fib = launcher.session_directory / "fib"
    for epoch in ("fip_2", "fip_1", "not_an_epoch"):
        (fib / epoch).mkdir(parents=True)
    calls: list[tuple[Path, Path]] = []
    monkeypatch.setattr(aind_physiology_fip.data_qc, "DataQcCli", RecordingQc(calls))

    fip.run_data_qc(launcher)

    assets = launcher.session_directory / "FIB" / "fip_qc_assets"
    assert calls == [(fib / "fip_1", assets), (fib / "fip_2", assets)]


def test_a_qc_failure_is_reported_not_raised(monkeypatch, launcher: FakeLauncher):
    (launcher.session_directory / "fib" / "fip_1").mkdir(parents=True)
    monkeypatch.setattr(aind_physiology_fip.data_qc, "DataQcCli", RecordingQc([], fail_on="fip_1"))

    fip.run_data_qc(launcher)  # must not raise: QC never aborts a session
