"""icons.json: hassfest's rules, and every entry reaches an entity of the model."""

from __future__ import annotations

import json
from pathlib import Path
import re

import pytest

from custom_components.deye_local_custom.coordinator import platform_of
from custom_components.deye_local_custom.models import MODELS

ICONS = json.loads(
    Path("custom_components/deye_local_custom/icons.json").read_text(encoding="utf-8")
)["entity"]
MDI = re.compile(r"^mdi:[a-z0-9-]+$")
# hassfest's translation key rule, which it applies to state keys too
TRANSLATION_KEY = re.compile(r"^(?!.+[_-]{2})(?![_-])[a-z0-9-_]+(?<![_-])$")


def test_hassfest_rules() -> None:
    for platform, entries in ICONS.items():
        for key, icons in entries.items():
            assert TRANSLATION_KEY.match(key), f"{platform}.{key}"
            assert MDI.match(icons["default"]), f"{platform}.{key}"
            for state, icon in (icons.get("state") or {}).items():
                assert TRANSLATION_KEY.match(state), f"{platform}.{key}: state {state}"
                assert MDI.match(icon)
                assert icon != icons["default"], f"{platform}.{key}: state {state}"


@pytest.mark.parametrize("model", MODELS, ids=lambda m: m.key)
def test_every_icon_reaches_an_entity(model) -> None:
    """Home Assistant finds icons by translation key: one each, the right platform."""
    keys = {(str(platform_of(d)), d.translation_key) for d in model.descriptions}
    for platform, entries in ICONS.items():
        for key in entries:
            assert (platform, key) in keys, f"{platform}.{key}: no such translation key"


@pytest.mark.parametrize("model", MODELS, ids=lambda m: m.key)
def test_state_icons(model) -> None:
    for d in model.sensors:
        if not d.state_icons:
            continue
        default = ICONS["sensor"][d.translation_key]["default"]
        assert set(d.state_icons) <= set(d.options or ()), d.key
        assert all(MDI.match(i) and i != default for i in d.state_icons.values())
