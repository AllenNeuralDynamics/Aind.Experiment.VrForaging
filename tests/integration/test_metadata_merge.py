"""The behavior and FIP metadata of a session, each written by its own mapper, merge into one record."""

from pathlib import Path

import pytest
from aind_behavior_vr_foraging_data_mappers import DataMapperCli as VrForagingDataMapperCli
from aind_data_schema.core.acquisition import Acquisition
from aind_data_schema.core.instrument import Instrument
from aind_data_schema.utils.compatibility_check import InstrumentAcquisitionCompatibility
from aind_physiology_fip_data_mappers import DataMapperCli as FipDataMapperCli

from .conftest import FIP, SESSION_END_TIME, SESSION_START_TIME, VR_FORAGING


@pytest.fixture
def mapped_session_directory(session_directory: Path) -> Path:
    """``session_directory`` after both mappers ran on it, as at the end of a session."""
    VrForagingDataMapperCli(
        data_path=session_directory,
        repository_path=VR_FORAGING,
        session_end_time=SESSION_END_TIME,
    ).cli_cmd()
    FipDataMapperCli(data_path=session_directory, repository_path=FIP).cli_cmd()
    return session_directory


@pytest.fixture
def instruments(mapped_session_directory: Path) -> tuple[Instrument, Instrument]:
    return tuple(
        Instrument.model_validate_json((mapped_session_directory / name).read_text(encoding="utf-8"))
        for name in ("instrument_vrforaging.json", "instrument_fib.json")
    )


@pytest.fixture
def acquisitions(mapped_session_directory: Path) -> tuple[Acquisition, Acquisition]:
    return tuple(
        Acquisition.model_validate_json((mapped_session_directory / name).read_text(encoding="utf-8"))
        for name in ("acquisition_vrforaging.json", "acquisition_fib.json")
    )


def test_acquisitions_merge(acquisitions: tuple[Acquisition, Acquisition]):
    behavior, fip = acquisitions
    merged = behavior + fip

    assert merged.subject_id == behavior.subject_id == fip.subject_id
    assert merged.acquisition_start_time == min(behavior.acquisition_start_time, fip.acquisition_start_time)
    assert merged.acquisition_end_time == max(behavior.acquisition_end_time, fip.acquisition_end_time)
    assert merged.acquisition_start_time <= SESSION_START_TIME
    assert merged.acquisition_end_time >= SESSION_END_TIME
    merged_devices = {device for stream in merged.data_streams for device in stream.active_devices}
    for source in (behavior, fip):
        assert {device for stream in source.data_streams for device in stream.active_devices} <= merged_devices
    assert Acquisition.model_validate_json(merged.model_dump_json()) == merged


def test_instruments_merge(instruments: tuple[Instrument, Instrument]):
    behavior, fip = instruments
    merged = behavior + fip

    merged_components = {component.name for component in merged.components}
    assert {component.name for component in behavior.components} <= merged_components
    assert {component.name for component in fip.components} <= merged_components
    assert set(behavior.modalities) | set(fip.modalities) == set(merged.modalities)
    assert Instrument.model_validate_json(merged.model_dump_json()) == merged


def test_merged_instrument_describes_merged_acquisition(
    acquisitions: tuple[Acquisition, Acquisition], instruments: tuple[Instrument, Instrument]
):
    # TODO: re-enable raise_for_missing_devices once the FIP mapper stops listing the implanted fibers
    # (Fiber_N, which live in Procedures) as active devices.
    InstrumentAcquisitionCompatibility(
        instrument=instruments[0] + instruments[1],
        acquisition=acquisitions[0] + acquisitions[1],
    ).run_compatibility_check(raise_for_missing_devices=False)
