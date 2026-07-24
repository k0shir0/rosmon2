"""Process state model used by the supervisor and terminal UI."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class State(Enum):
    """Visible process states, matching rosmon's state vocabulary."""

    IDLE = 'idle'
    RUNNING = 'running'
    CRASHED = 'crashed'
    WAITING = 'waiting'


@dataclass
class ProcessRecord:
    """All information needed to display and restart one launch process."""

    key: int
    display_name: str
    state: State = State.WAITING
    action: object = None
    cmd: List[str] = field(default_factory=list)
    cwd: Optional[str] = None
    env: Optional[Dict[str, str]] = None
    pid: Optional[int] = None
    muted: bool = False
    manually_stopped: bool = False
    restart_count: int = 0
    return_code: Optional[int] = None


SELECTION_ALPHABET = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'


def selection_key(index: int) -> Optional[str]:
    """Return rosmon's a-z, A-Z, 0-9 selection key for an index."""
    if 0 <= index < len(SELECTION_ALPHABET):
        return SELECTION_ALPHABET[index]
    return None


def namespace_of(display_name: str) -> str:
    """Return the top-level ROS namespace containing a display name."""
    parts = [part for part in display_name.strip('/').split('/') if part]
    return parts[0] if len(parts) > 1 else '/'


def full_name(display_name: str) -> str:
    """Return the absolute form of a display name, as reported over the wire."""
    return '/' + display_name.lstrip('/')
