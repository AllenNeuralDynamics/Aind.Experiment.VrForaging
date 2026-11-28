"""FIP: its rig library, app, mapper and QC, and the FIP-only workflows (acquisition and photobleaching)."""

import logging
from pathlib import Path

from aind_behavior_services.session import Session
from aind_physiology_fip.rig import AindPhysioFipRig
from aind_physiology_fip_data_mappers import DataMapperCli as FipDataMapperCli
from clabe import checks, steps, ui
from clabe.apps import AindBehaviorServicesBonsaiApp, CommandResult
from clabe.launcher import Launcher
from clabe.logging import otel
from clabe.stores import Kind, LocalFileStore
from clabe.workflow import Workflow

from .common import EXIT_PROMPT_TIMEOUT_S, copy_logs, run_data_transfer

logger = logging.getLogger(__name__)

_CONFIG_LIBRARY = Path(r"\\allen\aind\scratch\AindBehavior.db\AindPhysiologyFip")
_RIG = Kind.from_rig(AindPhysioFipRig)
_WORKFLOW = Path(r"./Aind.Physiology.Fip/src/main.bonsai")
_PHOTOBLEACHING_WORKFLOW = Path(r"./Aind.Physiology.Fip/src/photobleaching.bonsai")
_BONSAI_EXECUTABLE = Path(r"./Aind.Physiology.Fip/.bonsai/bonsai.exe")


def resolve_rig() -> AindPhysioFipRig:
    """Prompts for / loads a FIP rig from its configuration library."""
    return LocalFileStore(_CONFIG_LIBRARY).resolve(_RIG)


def fip_app(launcher: Launcher, rig: AindPhysioFipRig, session: Session) -> AindBehaviorServicesBonsaiApp:
    """The FIP acquisition Bonsai workflow."""
    return AindBehaviorServicesBonsaiApp(
        workflow=_WORKFLOW,
        executable=_BONSAI_EXECUTABLE,
        temp_directory=launcher.temp_dir,
        rig=rig,
        session=session,
        is_editor_mode=False,
    )


def run_mapper(launcher: Launcher) -> None:
    """Runs the FIP instrument and acquisition mappers (``instrument_fib.json`` / ``acquisition_fib.json``)."""
    if not (launcher.session_directory / "fib").exists():
        logger.info("No 'fib' folder found under %s; skipping FIP mapper.", launcher.session_directory)
        return
    assert launcher.repository.working_tree_dir is not None
    ui.notify("Running FIP data mappers...", ui.MessageLevel.INFO)
    FipDataMapperCli(
        data_path=launcher.session_directory,
        repository_path=Path(launcher.repository.working_tree_dir) / "Aind.Physiology.Fip",
    ).cli_cmd()


def run_data_qc(launcher: Launcher) -> None:
    """Runs FIP QC when the session contains FIP recordings. Failures are reported, not raised."""
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


# FIP acquisition without the VrForaging behavior -------------------------------------------------------------------

fip = Workflow(
    "fip",
    description="FIP photometry on its own, without the behavior.",
    order=5,
    exit_prompt_timeout=EXIT_PROMPT_TIMEOUT_S,
)

fip.step(steps.session())
fip.step(resolve_rig, name="rig", title="FIP rig")
fip.step(steps.preflight(checks.free_space(2e11)))
fip.step(steps.register())


@fip.step(cancellable=True)
async def acquire(launcher: Launcher, session: Session, rig: AindPhysioFipRig) -> CommandResult:
    return await fip_app(launcher, rig, session).run_async()


@fip.step(title="FIP mapper")
def mapper(launcher: Launcher) -> None:
    run_mapper(launcher)


@fip.step(title="FIP QC")
def qc(launcher: Launcher) -> None:
    run_data_qc(launcher)


fip.step(copy_logs)


@fip.step(optional="Would you like to transfer data?")
def transfer(launcher: Launcher, session: Session) -> None:
    run_data_transfer(launcher, session)


# Photobleaching: the workflow only takes the rig (no session, data, mappers, QC or transfer). After a success it
# exits without waiting for the user to close the session; after a failure it waits, so the error stays on screen.

fip_photobleaching = Workflow(
    "fip-photobleaching",
    description="Photobleaches the FIP fibers: light on, nothing recorded.",
    order=6,
    exit_prompt_timeout=0,
)

fip_photobleaching.step(resolve_rig, name="rig", title="FIP rig")


@fip_photobleaching.step
async def photobleach(launcher: Launcher, rig: AindPhysioFipRig) -> CommandResult:
    return await AindBehaviorServicesBonsaiApp(
        workflow=_PHOTOBLEACHING_WORKFLOW,
        executable=_BONSAI_EXECUTABLE,
        temp_directory=launcher.temp_dir,
        rig=rig,
        is_editor_mode=False,
    ).run_async()
