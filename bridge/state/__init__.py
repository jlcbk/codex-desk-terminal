"""bridge.state：纯 State Engine（P1.1）。见 reducer.py / render.py / engine.py。"""

from . import reducer  # noqa: F401
from .engine import StateEngine  # noqa: F401
from .reducer import new_internal_state  # noqa: F401
