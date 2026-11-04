"""Exception-safe CARLA actor cleanup without importing CARLA."""

from __future__ import annotations

from typing import Any


def destroy_actors(world: Any, actors: list[Any], tick_after: bool = False) -> list[str]:
    errors: list[str] = []
    seen: set[int] = set()
    for actor in list(actors):
        actor_identity = id(actor)
        if actor_identity in seen:
            continue
        seen.add(actor_identity)
        try:
            actor.stop()
        except AttributeError:
            pass
        except Exception as exc:
            errors.append(f"{getattr(actor, 'id', actor_identity)}.stop: {exc}")
        try:
            result = actor.destroy()
            if result is False:
                errors.append(f"{getattr(actor, 'id', actor_identity)}.destroy returned false")
        except Exception as exc:
            errors.append(f"{getattr(actor, 'id', actor_identity)}.destroy: {exc}")
    if tick_after and actors:
        try:
            world.tick()
        except Exception as exc:
            errors.append(f"cleanup_tick: {exc}")
    return errors

