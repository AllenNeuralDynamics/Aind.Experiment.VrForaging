"""CLABE launcher registry for Aind.Behavior.VrForaging + Physiology experiments.

Run with the generic clabe CLI, e.g.:
    clabe run main.py
    python -m clabe.cli run main.py
"""

from experiments.calibrate_olfactometer import calibrate_olfactometer
from experiments.vr_foraging import (
    calibration_protocol,
    recover_session,
    vr_foraging_fip_protocol,
    vr_foraging_protocol,
)

__all__ = [
    "calibrate_olfactometer",
    "calibration_protocol",
    "recover_session",
    "vr_foraging_fip_protocol",
    "vr_foraging_protocol",
]
