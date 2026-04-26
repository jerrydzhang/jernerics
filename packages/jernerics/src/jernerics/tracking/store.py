from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

from jernerics_proto import Envelope


def encode_varint(value: int) -> bytes:
    """Encode an integer using variable-length encoding."""
    if value < 0:
        raise ValueError("Negative values are not supported.")
    result = bytearray()
    while value >= 0x80:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value)
    return bytes(result)


def decode_varint(stream: BinaryIO) -> int | None:
    """
    Decodes a varint.
    Returns None if the stream is empty at the start.
    Raises EOFError if the stream ends mid-sequence.
    """
    shift = 0
    result = 0
    while True:
        byte = stream.read(1)
        if not byte:
            if shift == 0:
                return None
            raise EOFError("Truncated varint: Stream ended before MSB was 0.")
        byte_value = ord(byte)
        result |= (byte_value & 0x7F) << shift
        if not (byte_value & 0x80):
            return result
        shift += 7


class TrackingWriter:
    def __init__(self, path: Path):
        self.path = path
        self.file = open(path, "ab")  # noqa: SIM115

    def __enter__(self) -> TrackingWriter:
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def write_envelope(self, envelope: Envelope) -> None:
        envelope_bytes = envelope.SerializeToString()
        length_prefix = encode_varint(len(envelope_bytes))
        self.file.write(length_prefix + envelope_bytes)
        self.file.flush()

    def close(self) -> None:
        self.file.close()


class TrackingReader:
    def __init__(self, path: Path):
        self.path = path
        self.file = open(path, "rb")  # noqa: SIM115

    def __enter__(self) -> TrackingReader:
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def __iter__(self) -> Iterator[Envelope]:
        while True:
            envelope = self.read_envelope()
            if envelope is None:
                break
            yield envelope

    def read_envelope(self) -> Envelope | None:
        length = decode_varint(self.file)
        if length is None:
            return None
        envelope_bytes = self.file.read(length)
        if len(envelope_bytes) < length:
            raise EOFError(
                f"Truncated envelope: Expected {length} bytes,"
                f" got {len(envelope_bytes)}."
            )
        envelope = Envelope()
        envelope.ParseFromString(envelope_bytes)
        return envelope

    def close(self) -> None:
        self.file.close()
