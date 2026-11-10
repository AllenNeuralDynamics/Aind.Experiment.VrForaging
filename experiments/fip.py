"""FIP acquisition experiment and its helpers."""

import logging
from pathlib import Path

import aind_physiology_fip.rig
from aind_physiology_fip.data_mappers import ProtoAcquisitionMapper
from clabe import resource_monitor, ui
from clabe.apps import AindBehaviorServicesBonsaiApp
from clabe.launcher import Launcher, experiment
from clabe.logging import otel
from clabe.session import SessionBuilder
from clabe.stores import Kind, LocalFileStore

from .launcher_helpers import run_data_transfer

logger = logging.getLogger(__name__)

_FIP_CONFIG_LIBRARY = Path(r"\\allen\aind\scratch\AindBehavior.db\AindPhysiologyFip")
_FIP_RIG = Kind.from_rig(aind_physiology_fip.rig.AindPhysioFipRig)
FIP_WORKFLOW = Path(r"./Aind.Physiology.Fip/src/main.bonsai")
FIP_BONSAI_EXECUTABLE = Path(r"./Aind.Physiology.Fip/.bonsai/bonsai.exe")


def resolve_fip_rig() -> aind_physiology_fip.rig.AindPhysioFipRig:
    """Prompt for / load a FIP rig from the local configuration library."""
    return LocalFileStore(_FIP_CONFIG_LIBRARY).resolve(_FIP_RIG)


def run_fip_mapper(launcher: Launcher) -> None:
    if not (launcher.session_directory / "fib").exists():
        logger.info("No 'fib' folder found under %s; skipping FIP mapper.", launcher.session_directory)
        return
    fip_extracted = ProtoAcquisitionMapper(launcher.session_directory).map()
    (launcher.session_directory / "fip.json").write_text(fip_extracted.model_dump_json(indent=2), encoding="utf-8")


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


@experiment(name="fip", order=5)
async def fip_protocol(launcher: Launcher) -> None:
    """Run FIP acquisition without the VrForaging behavior component."""
    session = SessionBuilder(launcher).build()
    rig = resolve_fip_rig()

    resource_monitor.ResourceMonitor(
        constrains=[resource_monitor.available_storage_constraint_factory(rig.data_directory, 2e11)]
    ).run()
    launcher.register_session(session, rig.data_directory)

    await AindBehaviorServicesBonsaiApp(
        workflow=FIP_WORKFLOW,
        executable=FIP_BONSAI_EXECUTABLE,
        temp_directory=launcher.temp_dir,
        rig=rig,
        session=session,
        is_editor_mode=False,
    ).run_async()

    run_fip_mapper(launcher)
    run_fip_data_qc(launcher)
    launcher.copy_logs()
    run_data_transfer(launcher, session)
    ui.notify("FIP experiment completed successfully.", ui.MessageLevel.SUCCESS)
