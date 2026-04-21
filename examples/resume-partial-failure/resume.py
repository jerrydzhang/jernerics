from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from jernerics._cli_helpers import load_config
from jernerics.dag import DAG

dag_file = str(pathlib.Path(__file__).parent / "dag.py")
config_file = sys.argv[1] if len(sys.argv) > 1 else "config_fixed.py"

dag = DAG(dag_file)
sweep = load_config(config_file)
config = sweep._base

results = dag.resume(config, config_index=0)
print("Resume results:")
for name, result in results.items():
    print(f"  {name}: {result}")

failed = [n for n, r in results.items() if isinstance(r, Exception)]
if failed:
    print(f"FAILED: {failed}")
    sys.exit(1)
else:
    print("ALL PASSED")
