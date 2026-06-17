#!/usr/bin/env bash
set -euo pipefail

just lint
just format-check
just typecheck
just test