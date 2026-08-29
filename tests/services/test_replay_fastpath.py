from __future__ import annotations

import copy
import json
import unittest

from wish_builder.contracts import decode_journal_event_bytes
from wish_builder.services.replay import _decode_replay_event

from .test_replay import graph_freeze_events


class ReplayFastPathEquivalenceTests(unittest.TestCase):
    def assert_equivalent(self, raw: bytes) -> None:
        strict = decode_journal_event_bytes(raw)
        event, canonical = _decode_replay_event(raw)

        self.assertEqual(strict.ok, event is not None, strict.report.render_text())
        if strict.ok:
            self.assertEqual(strict.value, event)
            assert event is not None
            self.assertEqual(raw == event.canonical_json_bytes(), canonical)
        else:
            self.assertFalse(canonical)

    def test_valid_transition_and_noncanonical_bytes_match_strict_decoder(self) -> None:
        event = graph_freeze_events()[0]
        canonical = event.canonical_json_bytes()
        value = json.loads(canonical)
        noncanonical = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()

        self.assert_equivalent(canonical)
        self.assert_equivalent(noncanonical)

    def test_hostile_transition_shapes_match_strict_decoder(self) -> None:
        canonical = graph_freeze_events()[0].canonical_json_bytes()
        primitive = json.loads(canonical)

        def encoded(mutator) -> bytes:
            value = copy.deepcopy(primitive)
            mutator(value)
            return (
                json.dumps(
                    value,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
                + b"\n"
            )

        hostile = (
            b"\xff\n",
            b'{"sequence":NaN}\n',
            canonical.replace(b'"sequence":1', b'"sequence":1,"sequence":1', 1),
            canonical.replace(
                b'"payload_type":"transition"',
                b'"payload_type":"transition","payload_type":"transition"',
                1,
            ),
            encoded(lambda value: value.__setitem__("unknown", True)),
            encoded(lambda value: value["payload"].__setitem__("unknown", True)),
            encoded(lambda value: value.__setitem__("sequence", True)),
            encoded(lambda value: value.__setitem__("sequence", 1.5)),
            encoded(lambda value: value.__setitem__("sequence", 2**63)),
            encoded(lambda value: value.__setitem__("event_type", "unknown")),
            encoded(lambda value: value.__setitem__("event_hash", "sha256:bad")),
            encoded(lambda value: value.__setitem__("actor_id", "bad\u202eactor")),
            encoded(
                lambda value: value.__setitem__(
                    "actor_id",
                    "x" * 20_000,
                )
            ),
            encoded(lambda value: value["payload"].__setitem__("evidence", [{}])),
        )

        for index, raw in enumerate(hostile):
            with self.subTest(index=index):
                self.assert_equivalent(raw)


if __name__ == "__main__":
    unittest.main()
