import argparse
import os
import sys

from .server import serve

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _is_loopback(host: str) -> bool:
    if not host:
        return True
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host in _LOOPBACK_HOSTS


def main():
    parser = argparse.ArgumentParser(description="Jernerics tracking server")
    parser.add_argument(
        "--db",
        default="jernerics.sqlite",
        help="Path to SQLite database (default: jernerics.sqlite)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host/address to bind to (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--http-port",
        type=int,
        default=8000,
        help="HTTP port to listen on (default: 8000)",
    )
    parser.add_argument(
        "--artifacts-dir",
        default=None,
        help="Directory to store artifact files (default: sibling of the database)",
    )
    parser.add_argument(
        "--allow-unauthenticated",
        action="store_true",
        help=(
            "Permit binding a non-loopback address without JERNERICS_API_KEY"
            " (insecure; local development only)"
        ),
    )
    args = parser.parse_args()

    api_key = os.environ.get("JERNERICS_API_KEY")
    loopback = _is_loopback(args.host)

    if not api_key and not loopback and not args.allow_unauthenticated:
        print(
            f"Refusing to bind {args.host}:{args.http_port} without an API key:"
            " anyone who can reach this host could read and modify all tracking"
            " data. Set JERNERICS_API_KEY to enable authentication, or pass"
            " --allow-unauthenticated for local development.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    print(f"Listening on {args.host}:{args.http_port}", file=sys.stderr)
    print(f"Database: {args.db}", file=sys.stderr)
    if api_key:
        print("API key authentication enabled", file=sys.stderr)
    elif loopback:
        print(
            "WARNING: authentication is DISABLED (no JERNERICS_API_KEY set);"
            " the bind is loopback-only. Set JERNERICS_API_KEY to enable"
            " authentication.",
            file=sys.stderr,
        )
    else:
        print(
            "WARNING: authentication is DISABLED on a non-loopback bind:"
            " anyone who can reach this host can read and modify all tracking"
            " data.",
            file=sys.stderr,
        )

    serve(
        args.db,
        host=args.host,
        http_port=args.http_port,
        api_key=api_key,
        artifacts_root=args.artifacts_dir,
    )


if __name__ == "__main__":
    main()
