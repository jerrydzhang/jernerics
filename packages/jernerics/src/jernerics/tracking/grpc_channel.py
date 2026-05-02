import grpc


def grpc_channel(addr: str) -> grpc.Channel:
    """Create a gRPC channel appropriate for the address.

    localhost/127.0.0.1 addresses use insecure_channel.
    Everything else uses secure_channel (TLS, e.g. through Tailscale Funnel).
    """
    host = addr.split(":")[0]
    if host in ("localhost", "127.0.0.1"):
        return grpc.insecure_channel(addr)
    return grpc.secure_channel(addr, grpc.ssl_channel_credentials())
