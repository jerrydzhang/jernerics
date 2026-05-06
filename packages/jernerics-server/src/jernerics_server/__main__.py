import argparse
import os
import signal
import sys

from .server import serve


def main():
    parser = argparse.ArgumentParser(description="Jernerics gRPC tracking server")
    parser.add_argument(
        "--db",
        default="jernerics.sqlite",
        help="Path to SQLite database (default: jernerics.sqlite)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=50051,
        help="Port to listen on (default: 50051)",
    )
    parser.add_argument(
        "--host",
        default="[::]",
        help="Host/address to bind to (default: [::])",
    )
    parser.add_argument(
        "--http-port",
        type=int,
        default=None,
        help="HTTP port for query endpoint (default: disabled)",
    )
    parser.add_argument(
        "--http-host",
        default=None,
        help="HTTP host to bind to (default: same as --host)",
    )
    parser.add_argument(
        "--dashboard-dir",
        default=None,
        help="Path to dashboard static files. Mounted at / on the HTTP server.",
    )
    args = parser.parse_args()

    api_key = os.environ.get("JERNERICS_API_KEY")
    server = serve(
        args.db,
        args.port,
        args.host,
        api_key=api_key,
        http_port=args.http_port,
        http_host=args.http_host,
        dashboard_dir=args.dashboard_dir,
    )
    print(f"Listening on {args.host}:{args.port}", file=sys.stderr)
    print(f"Database: {args.db}", file=sys.stderr)
    if api_key:
        print("API key authentication enabled", file=sys.stderr)
    if args.http_port:
        print(f"HTTP query endpoint on port {args.http_port}", file=sys.stderr)
    if args.dashboard_dir:
        print(f"Dashboard served from {args.dashboard_dir}", file=sys.stderr)

    signal.signal(signal.SIGINT, lambda *_: (server.stop(grace=2), sys.exit(0)))
    signal.signal(signal.SIGTERM, lambda *_: (server.stop(grace=2), sys.exit(0)))

    server.wait_for_termination()


if __name__ == "__main__":
    main()
