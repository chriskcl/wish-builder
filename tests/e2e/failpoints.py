from __future__ import annotations

from pathlib import Path

from wish_builder.adapters.fake import FakeEffectCrash


class CrashOnce:
    def __init__(self, point: str) -> None:
        self.point = point
        self.triggered = False

    def __call__(self, point: str, _: Path) -> None:
        if point == self.point and not self.triggered:
            self.triggered = True
            raise FakeEffectCrash(f"injected crash at {point}")
