import csv
from io import StringIO
from pathlib import Path
from app.settings import KEY, UNIT

ROOT = Path(__file__).resolve().parents[1]
HEADER = [KEY, '修订号', '提交时间', '验收日', '观察期天数', '单株款元', '逐株事件', '原始备注']
BOOK = f'{KEY},外部记录值({UNIT})\n'


def sheet(*rows):
    stream = StringIO(newline='')
    csv.writer(stream).writerows([HEADER, *rows])
    return stream.getvalue().encode()


def row(key, revision, cutoff, days, price, trees, note=''):
    return [key, str(revision), '2026-11-30T09:00:00+08:00', cutoff, str(days), str(price), trees, note]


def live(key='00001', revision=1, cutoff='2026-11-30', days=10, price=100):
    return row(key, revision, cutoff, days, price,
               '树A:2026-11-01:种植;树A:2026-11-15:巡检存活')


def post(client, path, payload):
    return client.post(path, json=payload)


def start(client, *rows):
    response = post(client, '/api/runs', {'data': sheet(*rows).decode(), 'book': BOOK})
    assert response.status_code == 200, response.text
    return response.json()['manifest']['run_id']


def test_preview_lists_due_against_paid(client):
    run_id = start(client, live())
    preview = client.get(f'/api/settle/{run_id}', params={'period': '2026-11'}).json()
    row, = preview['rows']
    assert (row['code'], row['due'], row['paid'], row['payable']) == ('TOP_UP', '100.00', '0.00', '100.00')
    assert preview['totals']['due'] == '100.00' and preview['totals']['top_up'] == '100.00'
    assert preview['next_period'] == '2026-12'


def test_signed_difference_becomes_paid(client):
    run_id = start(client, live())
    signed = post(client, f'/api/settle/{run_id}', {'period': '2026-11', 'note': '十一月签收'}).json()
    assert [item['amount'] for item in signed['signed']] == ['100.00']
    assert signed['totals']['paid'] == '100.00' and signed['totals']['due'] == '0.00'
    assert signed['totals']['settled'] == 1
    ledger = client.get('/api/settle').json()
    assert [(item['business_key'], item['period'], item['amount']) for item in ledger] == [('00001', '2026-11', '100.00')]
    assert client.get(f'/api/settle/{run_id}/signoffs').json()[0]['note'] == '十一月签收'


def test_retry_does_not_pay_twice(client):
    run_id = start(client, live())
    first = post(client, f'/api/settle/{run_id}', {'period': '2026-11'}).json()
    again = post(client, f'/api/settle/{run_id}', {'period': '2026-11'}).json()
    assert len(first['signed']) == 1 and again['signed'] == []
    assert [item['code'] for item in again['skipped']] == ['SETTLED']
    assert again['totals']['paid'] == '100.00'
    assert len(client.get('/api/settle').json()) == 1


def test_late_correction_is_clawed_back_next_period(client):
    run_id = start(client, live())
    post(client, f'/api/settle/{run_id}', {'period': '2026-11'})
    corrected = start(client, live(revision=2, price=60))
    preview = client.get(f'/api/settle/{corrected}').json()
    row, = preview['rows']
    assert (row['code'], row['due'], row['paid'], row['period']) == ('CLAWBACK', '-40.00', '100.00', '2026-12')
    signed = post(client, f'/api/settle/{corrected}', {}).json()
    assert [item['amount'] for item in signed['signed']] == ['-40.00']
    assert signed['totals']['paid'] == '60.00' and signed['totals']['due'] == '0.00'
    assert signed['totals']['clawback'] == '0.00'
    history = client.get('/api/settle').json()
    assert [(item['period'], item['kind'], item['amount']) for item in history] == [
        ('2026-11', '补付', '100.00'), ('2026-12', '扣回', '-40.00')]


def test_earlier_signoff_keeps_original_amount(client):
    run_id = start(client, live())
    post(client, f'/api/settle/{run_id}', {'period': '2026-11'})
    corrected = start(client, live(revision=2, price=60))
    post(client, f'/api/settle/{corrected}', {})
    original = client.get(f'/api/settle/{run_id}/signoffs').json()[0]
    assert (original['amount'], original['payable'], original['paid_before']) == ('100.00', '100.00', '0.00')
    latest = client.get(f'/api/settle/{corrected}/signoffs').json()[0]
    assert (latest['amount'], latest['payable'], latest['paid_before']) == ('-40.00', '60.00', '100.00')


def test_recomputed_retry_does_not_repeat_clawback(client):
    run_id = start(client, live())
    post(client, f'/api/settle/{run_id}', {'period': '2026-11'})
    corrected = start(client, live(revision=2, price=60))
    first = post(client, f'/api/settle/{corrected}', {}).json()
    again = post(client, f'/api/settle/{corrected}', {}).json()
    assert len(first['signed']) == 1 and again['signed'] == []
    assert len(client.get('/api/settle').json()) == 2


def test_blocked_key_is_not_signed(client):
    blocked = row('00002', 1, '2026-11-30', 10, 100, 'N/A')
    run_id = start(client, live(), blocked)
    preview = client.get(f'/api/settle/{run_id}', params={'period': '2026-11'}).json()
    held = next(item for item in preview['rows'] if item['key'] == '00002')
    assert (held['code'], held['payable'], held['due']) == ('BLOCKED', None, None)
    assert held['blocked']['code'] == 'REQUIRED'
    signed = post(client, f'/api/settle/{run_id}', {'period': '2026-11'}).json()
    assert [item['key'] for item in signed['signed']] == ['00001']
    assert [item['code'] for item in signed['skipped']] == ['BLOCKED']
    assert signed['totals']['blocked'] == 1


def test_backdated_signing_is_refused(client):
    run_id = start(client, live())
    post(client, f'/api/settle/{run_id}', {'period': '2026-12'})
    refused = post(client, f'/api/settle/{run_id}', {'period': '2026-11'})
    assert (refused.status_code, refused.json()['code']) == (409, 'PERIOD_ORDER')
    assert len(client.get('/api/settle').json()) == 1


def test_period_format_is_checked(client):
    run_id = start(client, live())
    bad = client.get(f'/api/settle/{run_id}', params={'period': '2026-13'})
    assert (bad.status_code, bad.json()['code']) == (422, 'PERIOD')


def test_reopened_old_batch_cannot_top_up_again(client):
    original = start(client, live(price=100))
    post(client, f'/api/settle/{original}', {'period': '2026-11'})
    corrected = start(client, live(revision=2, price=60))
    post(client, f'/api/settle/{corrected}', {})
    reopened = post(client, f'/api/settle/{original}', {}).json()
    assert reopened['signed'] == []
    assert [item['code'] for item in reopened['skipped']] == ['STALE_REVISION']
    assert reopened['totals']['paid'] == '60.00'
    assert len(client.get('/api/settle').json()) == 2
    assert client.get(f'/api/settle/{original}/signoffs').json()[0]['amount'] == '100.00'


def test_later_revision_tops_up_next_period(client):
    first = start(client, live(price=100))
    post(client, f'/api/settle/{first}', {'period': '2026-11'})
    mature = start(client, live(revision=2, price=140))
    row, = client.get(f'/api/settle/{mature}').json()['rows']
    assert (row['code'], row['due'], row['period']) == ('TOP_UP', '40.00', '2026-12')
    signed = post(client, f'/api/settle/{mature}', {}).json()
    assert signed['totals']['paid'] == '140.00' and signed['totals']['top_up'] == '0.00'


def test_concurrent_signing_keeps_one_record(client, source):
    from concurrent.futures import ThreadPoolExecutor
    from app.modules.settle.service import sign
    from app.modules.runs.service import run
    proof = run(source, BOOK.encode())
    run_id = proof['manifest']['run_id']
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: sign(run_id, '2026-11', '并发'), range(4)))
    assert sum(len(item['signed']) for item in results) == 18
    assert len({item['id'] for result in results for item in result['signed']}) == 18
    assert len(client.get('/api/settle').json()) == 18
    assert client.get(f'/api/settle/{run_id}').json()['totals']['paid'] == '3730.00'


def test_split_batches_do_not_double_pay_shared_keys(client):
    source = (ROOT/'sample_data/shift.csv').read_text(encoding='utf-8-sig').splitlines()
    header, body = source[0], source[1:]
    book = f'{KEY},外部记录值({UNIT})\n'
    first = post(client, '/api/runs', {'data': '\n'.join([header, *body[:9]]), 'book': book}).json()
    second = post(client, '/api/runs', {'data': '\n'.join([header, *body[9:]]), 'book': book}).json()
    paid = {}
    for run in (first, second):
        signed = post(client, f'/api/settle/{run["manifest"]["run_id"]}', {'note': '分批签收'}).json()
        for item in signed['signed']:
            assert item['key'] not in paid
            paid[item['key']] = item['amount']
    combined = post(client, '/api/runs', {'data': (ROOT/'sample_data/shift.csv').read_text(encoding='utf-8-sig'),
                                          'book': book}).json()
    preview = client.get(f'/api/settle/{combined["manifest"]["run_id"]}').json()
    assert preview['totals']['due'] == '0.00'
    assert preview['totals']['paid'] == '3730.00'


def test_blocked_key_keeps_paid_amount_visible(client):
    run_id = start(client, live())
    post(client, f'/api/settle/{run_id}', {'period': '2026-11'})
    blocked = row('00001', 2, '2026-11-30', 10, 100, 'N/A')
    broken = start(client, blocked)
    preview = client.get(f'/api/settle/{broken}', params={'period': '2026-11'}).json()
    held = next(item for item in preview['rows'] if item['key'] == '00001')
    assert (held['code'], held['paid'], held['payable']) == ('BLOCKED', '100.00', None)
    assert preview['totals']['held'] == '100.00' and preview['totals']['blocked'] == 1
    assert post(client, f'/api/settle/{broken}', {'period': '2026-11'}).json()['signed'] == []
    assert len(client.get('/api/settle').json()) == 1


def test_orphaned_payment_is_reported(client):
    first = start(client, live('00001'))
    post(client, f'/api/settle/{first}', {'period': '2026-11'})
    second = start(client, live('00002', price=50))
    preview = client.get(f'/api/settle/{second}', params={'period': '2026-11'}).json()
    assert preview['orphans'] == [{'key': '00001', 'paid': '100.00'}]
    assert preview['totals']['orphaned'] == '100.00'
