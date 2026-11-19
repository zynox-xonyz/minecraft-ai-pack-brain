"""Tiny CLI to try the tool layer: python -m packbrain.cli <tool> <args...>  (run from the repository root)."""
import json, sys
sys.stdout.reconfigure(encoding="utf-8")
from packbrain import tools

if __name__ == "__main__":
    fn = getattr(tools, sys.argv[1])
    args = [a for a in sys.argv[2:]]
    args = [int(a) if a.isdigit() else a for a in args]
    print(json.dumps(fn(*args), indent=1, ensure_ascii=False, default=str))
