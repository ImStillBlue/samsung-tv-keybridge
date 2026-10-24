"""Explicit routes used only by test fixtures."""

from typing import Final

from theatre.config import AppId, KeyMapping

HOME: Final = AppId("Theatre001.Home")
MOONFIN: Final = AppId("MoonfinApp.moonfin")
TARGETS: Final = frozenset((HOME, MOONFIN))
MAPPINGS: Final = (
    KeyMapping(key="home", app_id=HOME),
    KeyMapping(key="tv_plus", app_id=MOONFIN),
)
ROUTES: Final = {63: HOME, 560: MOONFIN}
