"""Replacement plugin entrypoint; registration is gated until M6/M7 are verified.

This isolated source tree must not load or advertise the obsolete controller.
"""


def register(ctx: object) -> None:
    """Refuse activation until the planned coordinator and release gates exist."""
    raise RuntimeError("replacement plugin is not ready for registration")
