from pathlib import Path
import csv

def write_attempts_csv(run_dir, attempts):
    p=Path(run_dir)/"attempts.csv"
    with p.open("w", newline="") as f:
        w=csv.writer(f); w.writerow(["attempt_id","backend","runner","status","valid","score","cost","input_tokens","output_tokens","diff"])
        for a in attempts:
            v=a.validation; w.writerow([a.attempt_id,a.backend_name,a.runner_name,a.status,bool(v and v.passed),v.score if v else "",a.estimated_cost,a.input_tokens,a.output_tokens,a.diff_path])
    return p
