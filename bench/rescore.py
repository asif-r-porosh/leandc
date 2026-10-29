"""Re-run verifiers over stored work dirs (no model calls). Used after fixing a verifier."""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from tasks import BENCH, TASKS

V = {t["id"]: t["verify"] for t in TASKS}
for f in sorted((BENCH / "runs").glob("*/summary.json")):
    s = json.load(open(f))
    if s["task"] not in V:
        continue
    new = bool(V[s["task"]](f.parent / "work", s.get("answer", "")))
    if new != s.get("passed"):
        print(f"{f.parent.name}: passed {s.get('passed')} -> {new}")
        s["passed"] = new
        f.write_text(json.dumps(s, indent=1))
print("rescored")
