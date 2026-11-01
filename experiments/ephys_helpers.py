"""Helpers for experiments that record with Open Ephys."""

import logging
from pathlib import Path

from aind_behavior_services.session import Session
from aind_physiology_openephys.app import AindBehaviorServicesOpenEphysApp
from aind_physiology_openephys.rig import AindPhysioOpenEphysRig
from aind_physiology_openephys_data_mappers import DataMapperCli as OpenEphysDataMapperCli
from clabe.stores import Kind, LocalFileStore
from clabe.utils import is_this_computer
from clabe.xml_rpc import to_unc

logger = logging.getLogger(__name__)

OPEN_EPHYS_CONFIG_LIBRARY = Path(r"\\allen\aind\scratch\AindBehavior.db\AindPhysiologyOpenEphys")
OPEN_EPHYS_RIG = Kind.from_rig(AindPhysioOpenEphysRig)


def find_open_ephys_session_directory(session: Session) -> Path | None:
    """The Open Ephys session directory of ``session``, as this computer reaches it, or None if none was recorded.

    Looks in the data directory of each Open Ephys rig configured for this computer, through its administrative share
    when the rig is another computer.
    """
    for open_ephys_rig in LocalFileStore(OPEN_EPHYS_CONFIG_LIBRARY).list(OPEN_EPHYS_RIG):
        path = AindBehaviorServicesOpenEphysApp(open_ephys_rig, session, skip_validation=True).gui_session_directory
        if not is_this_computer(open_ephys_rig.computer_name):
            path = to_unc(path, host=open_ephys_rig.computer_name)
        if Path(path).is_dir():
            return Path(path)
    return None


def run_open_ephys_mapper(session_directory: Path | None) -> None:
    """Map the Open Ephys data in ``session_directory``, skipping if no Open Ephys session was recorded."""
    if session_directory is None:
        logger.info("No Open Ephys session directory; skipping Open Ephys mapper.")
        return
    OpenEphysDataMapperCli(data_path=session_directory).cli_cmd()
