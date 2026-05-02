import argparse
import signal
import sys

from .server import serve


def main():
    parser = argparse.ArgumentParser(description="Jernerics gRPC tracking server")
    parser.add_argument(
        "--db",
        default="jernerics.duckdb",
        help="Path to DuckDB database (default: jernerics.duckdb)",
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
    args = parser.parse_args()

    server = serve(args.db, args.port, args.host)
    print(f"Listening on {args.host}:{args.port}", file=sys.stderr)
    print(f"Database: {args.db}", file=sys.stderr)

    signal.signal(signal.SIGINT, lambda *_: (server.stop(grace=2), sys.exit(0)))
    signal.signal(signal.SIGTERM, lambda *_: (server.stop(grace=2), sys.exit(0)))

    server.wait_for_termination()


if __name__ == "__main__":
    main()
