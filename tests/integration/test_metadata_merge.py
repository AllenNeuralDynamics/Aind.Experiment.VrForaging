"""The behavior and Open Ephys metadata of a session, each written by its own mapper, merge into one record."""

from pathlib import Path

import pytest
from aind_behavior_vr_foraging_data_mappers import DataMapperCli as VrForagingDataMapperCli
from aind_data_schema.core.acquisition import Acquisition
from aind_data_schema.core.instrument import Instrument
from aind_data_schema.utils.compatibility_check import InstrumentAcquisitionCompatibility
from aind_physiology_openephys_data_mappers import DataMapperCli as OpenEphysDataMapperCli

from .conftest import SESSION_END_TIME, SESSION_START_TIME, VR_FORAGING


@pytest.fixture
def mapped_session_directory(session_directory: Path) -> Path:
    """``session_directory`` after both mappers ran on it, as at the end of a session."""
    VrForagingDataMapperCli(
        data_path=session_directory,
        repository_path=VR_FORAGING,
        session_end_time=SESSION_END_TIME,
    ).cli_cmd()
    OpenEphysDataMapperCli(data_path=session_directory).cli_cmd()
    return session_directory


def _read(path: Path, model: type[Acquisition] | type[Instrument]):
    return model.model_validate_json(path.read_text(encoding="utf-8"))


@pytest.fixture
def acquisitions(mapped_session_directory: Path) -> tuple[Acquisition, Acquisition]:
    return (
        _read(mapped_session_directory / "acquisition_vrforaging.json", Acquisition),
        _read(mapped_session_directory / "ecephys_acquisition.json", Acquisition),
    )


@pytest.fixture
def instruments(mapped_session_directory: Path) -> tuple[Instrument, Instrument]:
    return (
        _read(mapped_session_directory / "instrument_vrforaging.json", Instrument),
        _read(mapped_session_directory / "ecephys_instrument.json", Instrument),
    )


def test_acquisitions_merge(acquisitions: tuple[Acquisition, Acquisition]):
    behavior, ephys = acquisitions
    merged = behavior + ephys

    assert merged.subject_id == behavior.subject_id == ephys.subject_id
    assert merged.acquisition_start_time == min(behavior.acquisition_start_time, ephys.acquisition_start_time)
    assert merged.acquisition_end_time == max(behavior.acquisition_end_time, ephys.acquisition_end_time)
    assert merged.acquisition_start_time <= SESSION_START_TIME
    assert merged.acquisition_end_time >= SESSION_END_TIME
    merged_devices = {device for stream in merged.data_streams for device in stream.active_devices}
    for source in (behavior, ephys):
        assert {device for stream in source.data_streams for device in stream.active_devices} <= merged_devices
    assert Acquisition.model_validate_json(merged.model_dump_json()) == merged


def test_instruments_merge(instruments: tuple[Instrument, Instrument]):
    behavior, ephys = instruments
    merged = behavior + ephys

    merged_components = {component.name for component in merged.components}
    assert {component.name for component in behavior.components} <= merged_components
    assert {component.name for component in ephys.components} <= merged_components
    assert set(behavior.modalities) | set(ephys.modalities) == set(merged.modalities)
    assert Instrument.model_validate_json(merged.model_dump_json()) == merged


def test_merged_instrument_describes_merged_acquisition(
    acquisitions: tuple[Acquisition, Acquisition], instruments: tuple[Instrument, Instrument]
):
    InstrumentAcquisitionCompatibility(
        instrument=instruments[0] + instruments[1],
        acquisition=acquisitions[0] + acquisitions[1],
    ).run_compatibility_check(raise_for_missing_devices=True)
