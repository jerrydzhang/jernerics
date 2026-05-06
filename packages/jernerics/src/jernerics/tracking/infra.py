import os
from typing import Any

import grpc


def resolve_streaming(
    server_addr: str,
) -> tuple[grpc.Channel, Any] | None:
    if not server_addr:
        return None

    from jernerics_proto import tracking_pb2_grpc

    from jernerics.tracking.grpc_channel import grpc_channel

    channel = grpc_channel(server_addr)
    stub = tracking_pb2_grpc.TrackingServiceStub(channel)
    return channel, stub


def resolve_artifact_storage() -> Any:
    import boto3

    bucket = os.environ.get("JERNERICS_ARTIFACT_BUCKET")
    endpoint = os.environ.get("AWS_ENDPOINT_URL")
    if not bucket or not endpoint:
        return None

    s3 = boto3.client("s3")

    def upload_file(s3_key: str, local_path: str) -> None:
        s3.upload_file(local_path, bucket, s3_key)

    return upload_file
