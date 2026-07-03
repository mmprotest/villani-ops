def render(result):
    v=result.get('verifier',{})
    lines=[f"Verifier mode: {'deterministic only' if v.get('mode')=='deterministic' else v.get('mode')}"]
    if v.get('mode')=='deterministic': lines.append('Warning: LLM verifier disabled; result is not authoritative.')
    else: lines.append(f"Verifier backend: {v.get('backend') or v.get('model')}")
    lines += [f"Verdict: {result['verdict']}",f"Confidence: {float(result.get('confidence',0)):.2f}",f"Recommended action: {result['recommendedAction']}",'','Reason:',str(result.get('reason',''))]
    return '\n'.join(lines)+'\n'
def exit_code(v): return {'success':0,'failure':1,'unclear':2,'error':3}.get(v,3)
