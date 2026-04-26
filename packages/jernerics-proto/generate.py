#!/usr/bin/env python
"""Regenerate protobuf + gRPC Python code from tracking.proto."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
PROTO = ROOT / "proto" / "tracking.proto"
OUT = ROOT / "src" / "jernerics_proto"
GRPC_FILE = OUT / "tracking_pb2_grpc.py"

subprocess.check_call(
    [
        sys.executable,
        "-m",
        "grpc_tools.protoc",
        f"--proto_path={ROOT / 'proto'}",
        f"--python_out={OUT}",
        f"--pyi_out={OUT}",
        f"--grpc_python_out={OUT}",
        PROTO.name,
    ]
)

text = GRPC_FILE.read_text()
text = text.replace(
    "import tracking_pb2 as tracking__pb2",
    "from jernerics_proto import tracking_pb2 as tracking__pb2",
)
GRPC_FILE.write_text(text)

print(f"Generated {', '.join(p.name for p in OUT.glob('tracking_pb2*'))}")
