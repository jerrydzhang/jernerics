import argparse
import os
import sys

from .server import serve


def main():
    parser = argparse.ArgumentParser(description="Jernerics tracking server")
    parser.add_argument(
        "--db",
        default="jernerics.sqlite",
        help="Path to SQLite database (default: jernerics.sqlite)",
    )
    parser.add_argument(
        "--host",
        default="[::]",
        help="Host/address to bind to (default: [::])",
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
    args = parser.parse_args()

    api_key = os.environ.get("JERNERICS_API_KEY")
    print(f"Listening on {args.host}:{args.http_port}", file=sys.stderr)
    print(f"Database: {args.db}", file=sys.stderr)
    if api_key:
        print("API key authentication enabled", file=sys.stderr)

    serve(
        args.db,
        host=args.host,
        http_port=args.http_port,
        api_key=api_key,
        artifacts_root=args.artifacts_dir,
    )


if __name__ == "__main__":
    main()
