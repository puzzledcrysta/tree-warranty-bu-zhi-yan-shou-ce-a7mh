import pytest


@pytest.fixture
def anchor(client):
    return client.post('/api/runs',json={}).json()['manifest']['run_id']


def review(client,anchor,version=0,decision='确认',note='已逐条核对外部账'):
    return client.post(f'/api/review/{anchor}',json={'version':version,'decision':decision,'note':note})


def test_stale(client,anchor):
    # TRAP-26
    assert review(client,anchor).json()['version']==1
    stale = review(client,anchor)
    assert (stale.status_code,stale.json()['code'])==(409,'STALE')


def test_noted_differences(client,anchor):
    response = review(client,anchor,note='  ')
    assert (response.status_code,response.json()['code'])==(422,'REVIEW_NOTE')


def test_versioned_history(client,anchor):
    assert review(client,anchor).status_code == 200
    assert review(client,anchor,1,'退回','请补来源旁证').status_code == 200
    assert [item['version'] for item in client.get(f'/api/review/{anchor}').json()] == [2,1]


def test_export_reproducible(client,anchor):
    first = client.get(f'/api/export/{anchor}')
    second = client.get(f'/api/export/{anchor}')
    assert first.content == second.content and 'attachment' in first.headers['content-disposition']


def test_export_carries_source_evidence(client,anchor):
    proof = client.get(f'/api/export/{anchor}').json()
    row = proof['results'][0]
    assert (row['key'],row['line'])==('00101',2)
    assert row['raw'] and row['proof'] and proof['quarantine']


def test_decision_reaches_export(client,anchor):
    assert review(client,anchor).status_code==200
    assert client.get(f'/api/export/{anchor}').json()['review'][0]['decision']=='确认'


def test_export_carries_signed_amounts(client,anchor):
    assert client.get(f'/api/export/{anchor}').json()['signoffs']==[]
    assert client.post(f'/api/settle/{anchor}',json={'period':'2026-11','note':'签收'}).json()['signed']
    proof = client.get(f'/api/export/{anchor}').json()
    first = next(item for item in proof['signoffs'] if item['business_key']=='00101')
    assert (first['period'],first['amount'],first['note'])==('2026-11','240.00','签收')
    assert len(proof['signoffs'])==18


def test_export_keeps_earlier_signoffs(client,anchor):
    client.post(f'/api/settle/{anchor}',json={'period':'2026-11'})
    from pathlib import Path
    payload = Path(__file__).resolve().parents[1]/'sample_data/shift.csv'
    text = payload.read_text(encoding='utf-8-sig').replace(
        '00101,1,2026-11-18T09:30:00+08:00,2026-11-18,20,240,',
        '00101,2,2026-11-30T09:30:00+08:00,2026-11-18,20,200,')
    run_id = client.post('/api/runs',json={'data':text}).json()['manifest']['run_id']
    proof = client.get(f'/api/export/{run_id}').json()
    rows = [item for item in proof['signoffs'] if item['business_key']=='00101']
    assert [(item['period'],item['amount'],item['payable']) for item in rows]==[('2026-11','240.00','240.00')]
