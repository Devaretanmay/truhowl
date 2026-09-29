from .koyote import Koyote, AgentKoyote, KoyoteConfig, KoyoteResult
from .compartments import Compartment, CompartmentConfig

from . import compartments as compartments
from . import hooks as hooks
from . import autopatch as autopatch
from . import graph as graph
from . import audit as audit
from . import maintenance as maintenance
from . import maintenance_agents as maintenance_agents
from . import hunt as hunt
from . import hunt_ports as hunt_ports
from . import redact as redact
from . import pipeline as pipeline
from . import github as github

try:
    from importlib.metadata import version as _package_version
    __version__ = _package_version("koyote")
except Exception:
    __version__ = "1.1.3"

__all__ = [
    "Koyote",
    "AgentKoyote",
    "KoyoteConfig",
    "KoyoteResult",
    "Compartment",
    "CompartmentConfig",
    "compartments",
    "hooks",
    "autopatch",
    "graph",
    "audit",
    "maintenance",
    "maintenance_agents",
    "hunt",
    "hunt_ports",
    "redact",
    "pipeline",
    "github",
]


