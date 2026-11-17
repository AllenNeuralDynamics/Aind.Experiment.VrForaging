"""The metadata of a session's modalities, each written by its own mapper, merges into one record."""

import functools
import operator
from pathlib import Path

import pytest
from aind_behavior_vr_foraging_data_mappers import DataMapperCli as VrForagingDataMapperCli
from aind_data_schema.core.acquisition import Acquisition
from aind_data_schema.core.instrument import Instrument
from aind_data_schema.utils.compatibility_check import InstrumentAcquisitionCompatibility
from aind_physiology_fip_data_mappers import DataMapperCli as FipDataMapperCli
from aind_physiology_openephys_data_mappers import DataMapperCli as OpenEphysDataMapperCli

from .conftest import FIP, SESSION_END_TIME, SESSION_START_TIME, VR_FORAGING


@pytest.fixture
def mapped_session_directory(session_directory: Path) -> Path:
    """``session_directory`` after both mappers ran on it, as at the end of a session."""
    VrForagingDataMapperCli(
        data_path=session_directory,
        repository_path=VR_FORAGING,
        session_end_time=SESSION_END_TIME,
    ).cli_cmd()
    OpenEphysDataMapperCli(data_path=session_directory).cli_cmd()
    FipDataMapperCli(data_path=session_directory, repository_path=FIP).cli_cmd()
    return session_directory


# Each modality's (instrument, acquisition) files, as written by its mapper
_METADATA_FILES = {
    "behavior": ("instrument_vrforaging.json", "acquisition_vrforaging.json"),
    "ephys": ("ecephys_instrument.json", "ecephys_acquisition.json"),
    "fip": ("instrument_fib.json", "acquisition_fib.json"),
}
_COMBINATIONS = {
    "behavior+ephys": ("behavior", "ephys"),
    "behavior+fip": ("behavior", "fip"),
    "behavior+fip+ephys": ("behavior", "fip", "ephys"),
}


def _read(path: Path, model: type[Acquisition] | type[Instrument]):
    return model.model_validate_json(path.read_text(encoding="utf-8"))


@pytest.fixture(params=_COMBINATIONS.values(), ids=_COMBINATIONS.keys())
def instruments_and_acquisitions(
    request: pytest.FixtureRequest, mapped_session_directory: Path
) -> tuple[list[Instrument], list[Acquisition]]:
    """The instruments and acquisitions of one combination of modalities."""
    files = [_METADATA_FILES[modality] for modality in request.param]
    return (
        [_read(mapped_session_directory / instrument, Instrument) for instrument, _ in files],
        [_read(mapped_session_directory / acquisition, Acquisition) for _, acquisition in files],
    )


def test_acquisitions_merge(instruments_and_acquisitions: tuple[list[Instrument], list[Acquisition]]):
    _, sources = instruments_and_acquisitions
    merged = functools.reduce(operator.add, sources)

    assert {source.subject_id for source in sources} == {merged.subject_id}
    assert merged.acquisition_start_time == min(source.acquisition_start_time for source in sources)
    assert merged.acquisition_end_time == max(source.acquisition_end_time for source in sources)
    assert merged.acquisition_start_time <= SESSION_START_TIME
    assert merged.acquisition_end_time >= SESSION_END_TIME
    merged_devices = {device for stream in merged.data_streams for device in stream.active_devices}
    for source in sources:
        assert {device for stream in source.data_streams for device in stream.active_devices} <= merged_devices
    assert Acquisition.model_validate_json(merged.model_dump_json()) == merged


def test_instruments_merge(instruments_and_acquisitions: tuple[list[Instrument], list[Acquisition]]):
    sources, _ = instruments_and_acquisitions
    merged = functools.reduce(operator.add, sources)

    merged_components = {component.name for component in merged.components}
    for source in sources:
        assert {component.name for component in source.components} <= merged_components
    assert set().union(*(source.modalities for source in sources)) == set(merged.modalities)
    assert Instrument.model_validate_json(merged.model_dump_json()) == merged


def test_merged_instrument_describes_merged_acquisition(
    instruments_and_acquisitions: tuple[list[Instrument], list[Acquisition]],
):
    instruments, acquisitions = instruments_and_acquisitions
    InstrumentAcquisitionCompatibility(
        instrument=functools.reduce(operator.add, instruments),
        acquisition=functools.reduce(operator.add, acquisitions),
    ).run_compatibility_check(raise_for_missing_devices=True)
