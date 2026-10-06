"""Standalone FIP acquisition experiment."""

from pathlib import Path

import aind_physiology_fip.rig
from clabe import resource_monitor, ui
from clabe.apps import AindBehaviorServicesBonsaiApp
from clabe.launcher import Launcher, experiment
from clabe.session import SessionBuilder
from clabe.stores import Kind, LocalFileStore

from .launcher_helpers import run_data_transfer, run_fip_data_qc, run_fip_mapper

_FIP_CONFIG_LIBRARY = Path(r"\\allen\aind\scratch\AindBehavior.db\AindPhysiologyFip")
_FIP_RIG = Kind.from_rig(aind_physiology_fip.rig.AindPhysioFipRig)


@experiment(name="fip", order=5)
async def fip_protocol(launcher: Launcher) -> None:
    """Run FIP acquisition without the VrForaging behavior component."""
    session = SessionBuilder(launcher).build()
    rig = LocalFileStore(_FIP_CONFIG_LIBRARY).resolve(_FIP_RIG)

    resource_monitor.ResourceMonitor(
        constrains=[resource_monitor.available_storage_constraint_factory(rig.data_directory, 2e11)]
    ).run()
    launcher.register_session(session, rig.data_directory)

    await AindBehaviorServicesBonsaiApp(
        workflow=Path(r"./Aind.Physiology.Fip/src/main.bonsai"),
        executable=Path(r"./Aind.Physiology.Fip/.bonsai/bonsai.exe"),
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
