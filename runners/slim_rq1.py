#!/usr/bin/env python3
"""Write slim copies of the RQ1 output files for the bundle: the same 136 file names, only the records RQ3
re-scores (status == "verified" and final_error == ""), only the fields it needs. Verification rates (Fig. 5)
are computed on the reference machine from the full files, so nothing is lost.

  python runners/slim_rq1.py <artifacts/rq1> <bundle>/inputs/rq1
"""
import glob
import json
import os
import sys

KEEP = ("task_id", "status", "final_error", "final_code", "class_name", "config")


def main(src, dst):
    os.makedirs(dst, exist_ok=True)
    files = recs = 0
    for f in sorted(glob.glob(os.path.join(src, "*.jsonl"))):
        with open(os.path.join(dst, os.path.basename(f)), "w") as out:
            for line in open(f):
                r = json.loads(line)
                if r.get("status") == "verified" and (r.get("final_error") or "") == "":
                    out.write(json.dumps({k: r.get(k) for k in KEEP}) + "\n")
                    recs += 1
        files += 1
    print(f"{files} files, {recs} records -> {dst}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
