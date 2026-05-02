import grpc

_KEEPALIVE_OPTIONS = [
    ("grpc.keepalive_time_ms", 30000),
    ("grpc.keepalive_timeout_ms", 10000),
    ("grpc.http2.max_pings_without_data", 0),
]


def grpc_channel(addr: str) -> grpc.Channel:
    """Create a gRPC channel appropriate for the address.

    localhost/127.0.0.1 addresses use insecure_channel.
    Everything else uses secure_channel (TLS, e.g. through Tailscale Funnel).
    """
    host = addr.split(":")[0]
    if host in ("localhost", "127.0.0.1"):
        return grpc.insecure_channel(addr, options=_KEEPALIVE_OPTIONS)
    return grpc.secure_channel(
        addr, grpc.ssl_channel_credentials(), options=_KEEPALIVE_OPTIONS
    )
