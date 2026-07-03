import json
def render(result):
    lines=[f"Verdict: {result['verdict']}",f"Confidence: {result['confidence']:.2f}",f"Recommended action: {result['recommendedAction']}",'','Reason:',result['reason'],'','Top success evidence:']
    lines += [f"{i}. {e['text']}" for i,e in enumerate(result.get('successEvidence',[])[:5],1)] or ['None']
    lines += ['','Active failure evidence:'] + ([f"{i}. {e['text']}" for i,e in enumerate(result.get('failureEvidence',[])[:5],1)] or ['None'])
    lines += ['','Recovered failures:'] + ([f"{i}. {e['text']}" for i,e in enumerate(result.get('recoveredFailures',[])[:5],1)] or ['None'])
    lines += ['','Missing evidence:'] + ([f"{i}. {e['text']}" for i,e in enumerate(result.get('missingEvidence',[])[:5],1)] or ['None'])
    return '\n'.join(lines)+'\n'
def exit_code(v): return {'success':0,'failure':1,'unclear':2,'error':3}.get(v,3)
