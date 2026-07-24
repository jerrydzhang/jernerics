from pathlib import Path

from jernerics.tracking.jsonl_io import TrackingReader, TrackingWriter

# The payload variants a JSONL envelope may carry (exactly one per line).
PAYLOAD_KEYS = {"param", "value", "artifact", "sweep_meta", "trial_end"}


def read_all(path: Path) -> list[dict]:
    with TrackingReader(path) as reader:
        return list(reader)


class TestRoundTrip:
    def test_single_dict(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        env = {"project": "p", "seq": 0, "value": {"key": "loss", "value": 0.5}}

        with TrackingWriter(p) as writer:
            writer.write_envelope(env)

        with TrackingReader(p) as reader:
            result = reader.read_envelope()
        assert result == env

    def test_read_envelope_returns_none_at_eof(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        p.write_text("")

        with TrackingReader(p) as reader:
            assert reader.read_envelope() is None

    def test_each_payload_variant_round_trips(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        envs = [
            {
                "project": "p",
                "seq": 0,
                "param": {"key": "lr", "value": {"float_val": 0.01}},
            },
            {
                "project": "p",
                "seq": 1,
                "value": {"key": "loss", "value": 0.5, "step": 10, "context": "{}"},
            },
            {
                "project": "p",
                "seq": 2,
                "value": {
                    "key": "pareto",
                    "value_json": "[1, 2]",
                    "step": None,
                    "context": "{}",
                },
            },
            {
                "project": "p",
                "seq": 3,
                "artifact": {"key": "model", "filename": "model.pt"},
            },
            {
                "project": "p",
                "seq": 4,
                "sweep_meta": {"git_hash": "abc", "config": "base = {}"},
            },
            {"project": "p", "seq": 5, "trial_end": {}},
        ]

        with TrackingWriter(p) as writer:
            for env in envs:
                writer.write_envelope(env)

        results = read_all(p)
        # Deep equality proves every dict shape survives the json round-trip.
        assert results == envs
        # Invariant: exactly one payload key per envelope.
        for env in results:
            assert len(PAYLOAD_KEYS & env.keys()) == 1


class TestTryReadEnvelope:
    def test_empty_file_returns_none(self, tmp_path: Path) -> None:
        p = tmp_path / "empty.jsonl"
        p.write_text("")

        with TrackingReader(p) as reader:
            assert reader.try_read_envelope() is None

    def test_blank_line_returns_none(self, tmp_path: Path) -> None:
        p = tmp_path / "blank.jsonl"
        p.write_text("\n\n")

        with TrackingReader(p) as reader:
            assert reader.try_read_envelope() is None

    def test_partial_line_returns_none_and_does_not_advance(
        self, tmp_path: Path
    ) -> None:
        p = tmp_path / "partial.jsonl"
        with TrackingWriter(p) as writer:
            writer.write_envelope(
                {"project": "p", "seq": 0, "value": {"key": "loss", "value": 0.5}}
            )
        # Simulate a writer mid-flush / crash: incomplete JSON, no trailing newline.
        with open(p, "a") as f:
            f.write('{"seq": 1,')

        with TrackingReader(p) as reader:
            # Consume the well-formed envelope first.
            first = reader.read_envelope()
            assert first is not None
            assert first["seq"] == 0
            # The partial line is unreadable and must not consume the cursor.
            pos_before = reader.file.tell()
            assert reader.try_read_envelope() is None
            assert reader.file.tell() == pos_before
            # Repeated peeks keep re-reading the same partial line (still None).
            assert reader.try_read_envelope() is None
            assert reader.file.tell() == pos_before


class TestAppendMode:
    def test_reopen_preserves_prior_data(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"

        with TrackingWriter(p) as writer:
            writer.write_envelope(
                {
                    "project": "p",
                    "seq": 0,
                    "param": {"key": "lr", "value": {"float_val": 0.01}},
                }
            )

        with TrackingWriter(p) as writer:
            writer.write_envelope(
                {"project": "p", "seq": 1, "value": {"key": "loss", "value": 0.5}}
            )

        results = read_all(p)
        assert len(results) == 2
        assert results[0]["param"]["key"] == "lr"
        assert results[1]["value"]["key"] == "loss"


class TestOrder:
    def test_many_writes_preserve_order(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"

        with TrackingWriter(p) as writer:
            for i in range(50):
                writer.write_envelope(
                    {
                        "project": "p",
                        "seq": i,
                        "value": {"key": "loss", "value": float(i)},
                    }
                )

        results = read_all(p)
        assert [r["seq"] for r in results] == list(range(50))
        assert [r["value"]["value"] for r in results] == [float(i) for i in range(50)]


class TestMixedPayloadTypes:
    def test_all_variants_round_trip_in_order(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        envs = [
            {
                "project": "p",
                "seq": 0,
                "param": {"key": "lr", "value": {"int_val": 64}},
            },
            {
                "project": "p",
                "seq": 1,
                "value": {"key": "loss", "value": 0.5, "step": 10, "context": "{}"},
            },
            {
                "project": "p",
                "seq": 2,
                "artifact": {"key": "model", "filename": "m.pt"},
            },
            {
                "project": "p",
                "seq": 3,
                "value": {
                    "key": "pareto",
                    "value_json": "[1, 2, 3]",
                    "step": None,
                    "context": "{}",
                },
            },
        ]

        with TrackingWriter(p) as writer:
            for env in envs:
                writer.write_envelope(env)

        results = read_all(p)
        assert len(results) == 4
        assert results == envs
        assert list(results[0].keys() & PAYLOAD_KEYS) == ["param"]
        assert list(results[1].keys() & PAYLOAD_KEYS) == ["value"]
        assert list(results[2].keys() & PAYLOAD_KEYS) == ["artifact"]
        assert list(results[3].keys() & PAYLOAD_KEYS) == ["value"]
