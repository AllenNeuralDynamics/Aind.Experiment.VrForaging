"""Fixtures that build a session directory as a ``vr-foraging-ephys`` session leaves it, ready to be mapped."""

import importlib.util
import shutil
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from types import ModuleType

import pytest
from aind_behavior_services.data_types import SoftwareEvent
from aind_behavior_services.session import Session
from aind_physiology_openephys.app import AindBehaviorServicesOpenEphysApp
from aind_physiology_openephys.rig import AindPhysioOpenEphysRig
from pydantic import BaseModel, TypeAdapter

REPOSITORY = Path(__file__).parents[2]
VR_FORAGING = REPOSITORY / "Aind.Behavior.VrForaging"
OPEN_EPHYS = REPOSITORY / "Aind.Physiology.OpenEphys"
# Recorded by the Open Ephys example dataset; the behavior session is mocked to run over the same interval.
SESSION_START_TIME = datetime.fromisoformat("2026-10-23T14:43:15.004-07:00")
SESSION_END_TIME = datetime.fromisoformat("2026-10-23T14:44:09.762-07:00")
REWARD_VOLUMES_UL = (5.0, 5.0)


def _load_example(name: str) -> ModuleType:
    """Load ``Aind.Behavior.VrForaging/examples/<name>.py``, which this repository's ``examples`` package shadows."""
    spec = importlib.util.spec_from_file_location(
        f"vr_foraging_examples.{name}", VR_FORAGING / "examples" / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_model(path: Path, model: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(model.model_dump_json(indent=2), encoding="utf-8")


def _write_events(path: Path, adapter: TypeAdapter, events: Sequence) -> None:
    """Serialize ``events`` as one-per-line JSON into ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(adapter.dump_json(event).decode("utf-8") + "\n" for event in events), encoding="utf-8")


def _write_software_events(behavior_dir: Path) -> None:
    time_event = TypeAdapter(SoftwareEvent[datetime])
    reward_event = TypeAdapter(SoftwareEvent[float])
    events_dir = behavior_dir / "SoftwareEvents"
    _write_events(
        events_dir / "StartSessionTime.json",
        time_event,
        [
            SoftwareEvent[datetime](
                name="StartSessionTime", timestamp=0.0, timestamp_source="harp", data=SESSION_START_TIME
            )
        ],
    )
    _write_events(
        events_dir / "EndSessionTime.json",
        time_event,
        [
            SoftwareEvent[datetime](
                name="EndSessionTime",
                timestamp=(SESSION_END_TIME - SESSION_START_TIME).total_seconds(),
                timestamp_source="harp",
                data=SESSION_END_TIME,
            )
        ],
    )
    _write_events(
        events_dir / "GiveReward.json",
        reward_event,
        [
            SoftwareEvent[float](name="GiveReward", timestamp=float(i), timestamp_source="harp", data=volume)
            for i, volume in enumerate(REWARD_VOLUMES_UL)
        ],
    )


@pytest.fixture
def session() -> Session:
    """The session shared by the behavior and Open Ephys recordings."""
    example = _load_example("session").session
    return example.model_copy(update={"date": SESSION_START_TIME, "session_name": "integration-session"})


@pytest.fixture
def session_directory(tmp_path: Path, session: Session) -> Path:
    """A session directory holding unmapped ``behavior`` and ``ecephys`` recordings of ``session``."""
    session_directory = tmp_path / "session"

    behavior_dir = session_directory / "behavior"
    _write_model(behavior_dir / "Logs" / "session_input.json", session)
    _write_model(behavior_dir / "Logs" / "rig_input.json", _load_example("rig").rig)
    _write_model(behavior_dir / "Logs" / "tasklogic_input.json", _load_example("task_patch_foraging").task_logic)
    _write_software_events(behavior_dir)

    ecephys_dir = session_directory / AindBehaviorServicesOpenEphysApp.MODALITY
    shutil.copytree(OPEN_EPHYS / "tests" / "example_data_directory", ecephys_dir)
    logs_dir = ecephys_dir / AindBehaviorServicesOpenEphysApp.LOGS
    _write_model(logs_dir / AindBehaviorServicesOpenEphysApp.SESSION_INPUT, session)
    _write_model(
        logs_dir / AindBehaviorServicesOpenEphysApp.RIG_INPUT,
        AindPhysioOpenEphysRig(
            rig_name="EPHYS-test",
            computer_name="EphysComputer",
            data_directory=Path("C:/data"),
            signal_chain=Path("src/example.xml"),
        ),
    )
    return session_directory
