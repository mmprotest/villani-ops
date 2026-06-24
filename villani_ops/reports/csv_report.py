from pathlib import Path
import csv

def write_attempts_csv(run_dir, attempts):
    p=Path(run_dir)/"attempts.csv"
    with p.open("w", newline="") as f:
        w=csv.writer(f); w.writerow(["attempt_id","backend","runner","status","valid","score","cost","input_tokens","output_tokens","coding_input_tokens","coding_output_tokens","token_accounting_status","debug_artifact_dir","duration_ms","model_requests","tool_calls","diff"])
        for a in attempts:
            if isinstance(a, dict):
                r=a.get('review') or {}; w.writerow([a.get('attempt_id'),a.get('backend_name'),a.get('runner_name'),a.get('status'),r.get('passed'),r.get('score'),a.get('coding_cost'),a.get('input_tokens'),a.get('output_tokens'),a.get('input_tokens'),a.get('output_tokens'),a.get('token_accounting_status'),a.get('debug_artifact_dir'),a.get('duration_ms'),a.get('model_requests'),a.get('total_tool_calls'),a.get('patch_path')])
            else:
                v=a.validation; w.writerow([a.attempt_id,a.backend_name,a.runner_name,a.status,bool(v and v.passed),v.score if v else "",a.estimated_cost,a.input_tokens,a.output_tokens,a.input_tokens,a.output_tokens,getattr(a,'token_accounting_status',''),getattr(a,'debug_artifact_dir',''),getattr(a,'duration_ms',''),getattr(a,'model_requests',''),getattr(a,'total_tool_calls',''),a.diff_path])
    return p
