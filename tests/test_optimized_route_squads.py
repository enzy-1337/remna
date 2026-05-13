"""Выдача internal squad sub-opt всем пользователям."""

from __future__ import annotations

from types import SimpleNamespace
from unittest import TestCase

from shared.services.optimized_route_service import (
    SUB_OPT_INTERNAL_SQUAD_UUID,
    remnawave_squads_for_db_user,
)


class OptimizedRouteSquadsTests(TestCase):
    def test_default_is_builtin_sub_opt(self) -> None:
        settings = SimpleNamespace(remnawave_optimized_squad_uuid=None)
        user = SimpleNamespace(optimized_route_enabled=False)
        self.assertEqual(
            remnawave_squads_for_db_user(settings, user),
            [SUB_OPT_INTERNAL_SQUAD_UUID],
        )

    def test_env_override(self) -> None:
        override = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        settings = SimpleNamespace(remnawave_optimized_squad_uuid=override)
        user = SimpleNamespace(optimized_route_enabled=False)
        self.assertEqual(remnawave_squads_for_db_user(settings, user), [override])

    def test_toggle_does_not_switch_squad_uuid(self) -> None:
        settings = SimpleNamespace(remnawave_optimized_squad_uuid=None)
        off = SimpleNamespace(optimized_route_enabled=False)
        on = SimpleNamespace(optimized_route_enabled=True)
        self.assertEqual(
            remnawave_squads_for_db_user(settings, off),
            remnawave_squads_for_db_user(settings, on),
        )

