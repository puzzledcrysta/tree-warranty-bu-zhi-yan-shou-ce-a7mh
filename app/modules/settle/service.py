import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from app.errors import DataError
from app.store import connection
from app.modules.runs.repository import lookup

ZERO = Decimal('0')
CENT = Decimal('0.01')
MONTH = re.compile(r'^[0-9]{4}-(?:0[1-9]|1[0-2])$')
KINDS = {'TOP_UP': '补付', 'CLAWBACK': '扣回', 'SETTLED': '结清', 'BLOCKED': '待核',
         'STALE_REVISION': '旧修订已结'}


def _period(value):
    if not MONTH.fullmatch(value.strip()):
        raise DataError('PERIOD', '结算期须使用 YYYY-MM')
    return value.strip()


def _shift(period, months):
    year, month = (int(part) for part in period.split('-'))
    index = year * 12 + month - 1 + months
    return f'{index // 12:04d}-{index % 12 + 1:02d}'


def _snapshot(value):
    return format(value.quantize(CENT), 'f')


def _paid(db):
    paid = {}
    for row in db.execute('SELECT business_key,amount FROM signoffs'):
        key, amount = row['business_key'], Decimal(row['amount'])
        paid[key] = paid.get(key, ZERO) + amount
    return paid


def _latest(db):
    latest = {}
    for row in db.execute('SELECT business_key,period,revision FROM signoffs ORDER BY period,created,id'):
        latest[row['business_key']] = {'period': row['period'], 'revision': row['revision']}
    return latest


def _blocked(run):
    taken = {row['key'] for row in run['accepted']}
    blocked = {}
    for row in sorted(run['quarantine'], key=lambda item: item['line']):
        key = row.get('key', '')
        if key and key not in taken and key not in blocked:
            blocked[key] = row
    return blocked


def _keys(run):
    return sorted({row['key'] for row in run['accepted']} |
                  {row.get('key', '') for row in run['quarantine'] if row.get('key')})


def _run_month(run):
    month = (run.get('manifest', {}).get('created') or '')[:7]
    return month if MONTH.fullmatch(month) else datetime.now(timezone.utc).strftime('%Y-%m')


def _base_period(run, period):
    if period is not None and str(period).strip():
        return _period(str(period))
    return _run_month(run)


def _rows(run, paid, latest, base):
    accepted = {row['key']: row for row in run['accepted']}
    blocked = _blocked(run)
    rows = []
    for key in sorted(set(accepted) | set(blocked)):
        already = paid.get(key, ZERO)
        seen = latest.get(key, {})
        last, after = seen.get('period'), seen.get('revision')
        period = base if last is None else max(base, _shift(last, 1))
        if key in accepted:
            row = accepted[key]
            payable = Decimal(row['amount'])
            due = payable - already
            code = 'SETTLED' if due == ZERO else 'TOP_UP' if due > ZERO else 'CLAWBACK'
            if due != ZERO and after is not None and row['revision'] < after:
                code = 'STALE_REVISION'
            rows.append({'key': key, 'line': row['line'], 'revision': row['revision'],
                         'paid': _snapshot(already), 'payable': _snapshot(payable),
                         'due': None if code == 'STALE_REVISION' else _snapshot(due),
                         'code': code, 'kind': KINDS[code],
                         'period': base if code == 'SETTLED' else period, 'blocked': None})
        else:
            row = blocked[key]
            rows.append({'key': key, 'line': row['line'], 'revision': row.get('revision'),
                         'paid': _snapshot(already), 'payable': None, 'due': None,
                         'code': 'BLOCKED', 'kind': KINDS['BLOCKED'], 'period': period,
                         'blocked': {'code': row['code'], 'detail': row['detail'], 'line': row['line']}})
    return rows


def _totals(rows, orphans):
    payable = [Decimal(row['payable']) for row in rows if row['code'] not in {'BLOCKED', 'STALE_REVISION'}]
    due = [Decimal(row['due']) for row in rows if row['code'] in {'TOP_UP', 'CLAWBACK', 'SETTLED'}]
    top = sum((value for value in due if value > ZERO), ZERO)
    claw = -sum((value for value in due if value < ZERO), ZERO)
    held = sum((Decimal(row['paid']) for row in rows if row['code'] == 'BLOCKED'), ZERO)
    return {'paid': _snapshot(sum((Decimal(row['paid']) for row in rows), ZERO)),
            'payable': _snapshot(sum(payable, ZERO)),
            'due': _snapshot(sum(due, ZERO)),
            'top_up': _snapshot(top), 'clawback': _snapshot(claw),
            'held': _snapshot(held),
            'orphaned': _snapshot(sum((Decimal(row['paid']) for row in orphans), ZERO)),
            'settled': sum(row['code'] == 'SETTLED' for row in rows),
            'blocked': sum(row['code'] == 'BLOCKED' for row in rows),
            'stale': sum(row['code'] == 'STALE_REVISION' for row in rows),
            'count': len(rows)}


def _orphans(paid, keys):
    present = set(keys)
    return [{'key': key, 'paid': _snapshot(value)}
            for key, value in sorted(paid.items()) if key not in present and value != ZERO]


def _signoffs(db, keys):
    if not keys:
        return []
    marks = ','.join('?' for _ in keys)
    rows = db.execute(f'SELECT * FROM signoffs WHERE business_key IN ({marks}) ORDER BY period,created,id',
                      tuple(keys))
    return [dict(row) for row in rows]


def _report(db, run_id, run, base):
    paid = _paid(db)
    keys = _keys(run)
    orphans = _orphans(paid, keys)
    rows = _rows(run, paid, _latest(db), base)
    return {'run_id': run_id, 'period': base,
            'next_period': _shift(max([base, *(row['period'] for row in rows)]), 1),
            'rows': rows, 'orphans': orphans, 'signoffs': _signoffs(db, keys),
            'totals': _totals(rows, orphans)}


def plan(run_id, period=None):
    with connection() as db:
        run = json.loads(lookup(db, run_id)['result'])
        return _report(db, run_id, run, _base_period(run, period))


def _check_period(rows, latest, base):
    # Reopening an already-signed earlier period would no longer match the payment vouchers.
    late = sorted(key for key in {row['key'] for row in rows}
                  if key in latest and latest[key]['period'] > base)
    if late:
        raise DataError('PERIOD_ORDER', f'{late[0]} 已在 {latest[late[0]]["period"]} 签收，'
                                        f'不能倒回 {base} 重签', 409)
    # Only a real difference can be misdated; settled or held keys carry no amount this period.
    for item in rows:
        if item['code'] in {'TOP_UP', 'CLAWBACK'} and item['period'] != base:
            raise DataError('PERIOD_ORDER', f'{item["key"]} 的差额应计入 {item["period"]}，'
                                            f'不能记在 {base}', 409)


def sign(run_id, period=None, note=''):
    note = (note or '').strip()[:500]
    explicit = period is not None and bool(str(period).strip())
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        run = json.loads(lookup(db, run_id)['result'])
        base = _base_period(run, period)
        latest = _latest(db)
        rows = _rows(run, _paid(db), latest, base)
        if explicit:
            _check_period(rows, latest, base)
        created = datetime.now(timezone.utc).isoformat()
        signed, skipped = [], []
        for item in rows:
            key, code = item['key'], item['code']
            if code == 'BLOCKED':
                skipped.append({'key': key, 'code': code, 'detail': '缺少可采信的复算结果'})
                continue
            if code == 'STALE_REVISION':
                skipped.append({'key': key, 'code': code, 'detail': '已有更高修订的签收，旧批次不再补付'})
                continue
            if code == 'SETTLED':
                skipped.append({'key': key, 'code': code, 'detail': '本期没有差额'})
                continue
            chosen = item['period']
            ident = sha256(f'settle:{key}:{chosen}'.encode()).hexdigest()
            cursor = db.execute('INSERT OR IGNORE INTO signoffs VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                                (ident, run_id, key, chosen, item['kind'], item['due'],
                                 item['paid'], item['payable'], item['revision'], note, created))
            if cursor.rowcount:
                signed.append({'id': ident, 'run_id': run_id, 'key': key, 'period': chosen,
                               'kind': item['kind'], 'code': code, 'amount': item['due'],
                               'paid_before': item['paid'], 'payable': item['payable'],
                               'revision': item['revision'], 'note': note, 'created': created})
            else:
                skipped.append({'key': key, 'code': 'DUPLICATE', 'detail': f'第 {chosen} 期差额已签收'})
        report = _report(db, run_id, run, base)
        report.update(note=note, created=created, signed=signed, skipped=skipped)
        return report


def signoffs_for_run(run_id):
    with connection() as db:
        lookup(db, run_id)
        return [dict(row) for row in db.execute(
            'SELECT * FROM signoffs WHERE run_id=? ORDER BY period,created,id', (run_id,))]


def history(period=None):
    with connection() as db:
        if period is not None and str(period).strip():
            chosen = _period(str(period))
            rows = db.execute('SELECT * FROM signoffs WHERE period=? ORDER BY period,created,id', (chosen,))
        else:
            rows = db.execute('SELECT * FROM signoffs ORDER BY period,created,id')
        return [dict(row) for row in rows]
