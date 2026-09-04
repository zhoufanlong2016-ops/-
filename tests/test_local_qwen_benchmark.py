import json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parents[1]/'tools'))
from run_local_qwen_benchmark import batches,build_batch_messages,run_records,validate_mapping

def rec(i='ZH_EN_001',cat='road_bridge',direction=('zh-CN','en'),text='K12+340处安装阀门。'):
    return {'id':i,'source_language':direction[0],'target_language':direction[1],'category':cat,'source_text':text,'reference_translation':'Install valve at K12+340.','required_terms':[{'source':'阀门','target':'valve'}],'protected_literals':['K12+340'],'review_status':'candidate','notes':''}
class Resp:
    def __init__(self,x): self.x=x
    def raise_for_status(self): pass
    def json(self): return {'choices':[{'message':{'content':self.x}}]}
class Client:
    def __init__(self,x): self.x=list(x); self.requests=[]
    def post(self,endpoint,**kw): self.requests.append(kw['json']); return Resp(self.x.pop(0))
def out(rows): return json.dumps({r['id']:'Install valve at K12+340.' for r in rows})

def test_batch_rules():
    rs=[rec(f'ZH_EN_{i:03}',text='x'*250) for i in range(5)]
    assert len(list(batches(rs)))==2
    assert len(list(batches([rec('a'),rec('b',direction=('en','zh-CN'))])))==2
def test_system_and_user_minimal():
    s,u=build_batch_messages([rec()]); assert 'zh-CN' in s and 'en' in s and 'road_bridge' in s; assert json.loads(u)=={'ZH_EN_001':'K12+340处安装阀门。'}
    for forbidden in ('reference_translation','protected_literals','category','source_language','target_language'): assert forbidden not in u
def test_validation_normal_and_id_errors():
    rs=[rec(),rec('ZH_EN_002')]; valid,err,_=validate_mapping(rs,out(rs)); assert set(valid)=={'ZH_EN_001','ZH_EN_002'} and not err
    _,err,_=validate_mapping(rs,json.dumps({'ZH_EN_001':'x','extra':'y'})); assert 'id_set_mismatch' in err
def test_missing_term_literal_and_invalid_json():
    _,e,_=validate_mapping([rec()],json.dumps({'ZH_EN_001':'Install gate.'})); assert any('protected' in x or 'required' in x for x in e)
    _,e,_=validate_mapping([rec()],'bad'); assert e==['invalid_json']
def test_batch_retry_and_single_fallback(tmp_path):
    rs=[rec(),rec('ZH_EN_002')]; c=Client(['bad','bad',out([rs[0]]),out([rs[1]])]); result=run_records(rs,c,tmp_path/'r.jsonl'); assert len(c.requests)==4 and all(x['success'] for x in result)
def test_partial_and_resume(tmp_path):
    rs=[rec(),rec('ZH_EN_002',cat='concrete')]; c=Client([out([rs[0]]),'bad','bad','bad','bad']); result=run_records(rs,c,tmp_path/'r.jsonl'); assert result[0]['success']; assert not result[1]['success']
    c2=Client([out([rs[1]])]); result=run_records(rs,c2,tmp_path/'r.jsonl'); assert c2.requests and result[0]['request_count']==1
def test_reference_not_prompt(tmp_path):
    r=rec(); c=Client([out([r])]); run_records([r],c,tmp_path/'r.jsonl'); assert r['reference_translation'] not in c.requests[0]['messages'][1]['content']
