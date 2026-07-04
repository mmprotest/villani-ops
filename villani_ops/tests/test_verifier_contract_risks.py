from villani_ops.verifier.llm import detect_contract_risks, needs_forced_inspection_before_accept

def pkt(txt='', active=0, recovered=0): return {'objective':txt,'deterministicChecks':{'activeFailureCount':active,'recoveredFailureCount':recovered},'evidence':{'x':[{'text':txt}]}}

def test_risk_detection_and_forced_inspection():
    p=pkt('make it faster with median runtime')
    risks=detect_contract_risks(p['objective'],p,{})
    assert any(r['kind']=='performance' for r in risks)
    assert needs_forced_inspection_before_accept({'result':1},risks,[],p)
    assert needs_forced_inspection_before_accept({'result':1},risks,[{'tool':'search_commands','args':{'query':'median runtime'}}],p) is None

def test_install_generated_constraints_failures_force():
    for text,kind in [('available in PATH after pip install','downstream_consumer'),('generate out.json','generated_output'),('only edit a.py and no warnings','allowed_edit')]:
        p=pkt(text); risks=detect_contract_risks(text,p,{})
        assert any(r['kind']==kind for r in risks)
        assert needs_forced_inspection_before_accept({'result':1},risks,[],p)
    p=pkt('done',active=1); risks=detect_contract_risks('done',p,{})
    assert any(r['kind']=='earlier_failures_conflict' for r in risks)
