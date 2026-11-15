"""Fall detection for the Myo armband, with alerts sent to a Jetson (or anything on the network)."""

from .detector import FallConfig, FallDetector, FallEvent

__all__ = ["FallConfig", "FallDetector", "FallEvent"]
__version__ = "0.1.0"
