"""CLABE workflows for Aind.Behavior.VrForaging + Physiology experiments.

Run with the generic clabe CLI, e.g.:
    uv run clabe run
    uv run clabe run --experiment-name vr-foraging-fip
"""

from experiments.fip import fip, fip_photobleaching
from experiments.olfactometer import calibrate_olfactometer, recover_olfactometer_calibration
from experiments.recover_session import recover_session
from experiments.vr_foraging import calibration, vr_foraging, vr_foraging_fip

__all__ = [
    "calibrate_olfactometer",
    "calibration",
    "fip",
    "fip_photobleaching",
    "recover_olfactometer_calibration",
    "recover_session",
    "vr_foraging",
    "vr_foraging_fip",
]
