import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "jetson")]

ROBOT_TOKEN = "robot-token-for-tests-123"
OPERATOR_TOKEN = "operator-token-for-tests-456"
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
