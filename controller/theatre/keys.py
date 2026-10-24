"""Hardware-verified keys permitted in user mappings."""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Literal

KeyName = Literal["home", "tv_plus"]
# Add names only after their code has been verified with discovery mode.
SAFE_KEYS: Final[Mapping[KeyName, int]] = MappingProxyType({"home": 63, "tv_plus": 560})
