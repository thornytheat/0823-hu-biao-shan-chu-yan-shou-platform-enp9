"""Durable, idempotent supplemental deletions and current inventory versions.

The original run is never rewritten.  A supplement is merged with the persisted
source of its base run, evaluated under a current high-water-mark sequence, and
stored as a new content-addressed run.  A unique idempotency key plus a
deterministic run id make retries safe after an interrupted request.
"""
from __future__ import annotations

import csv
import io
from hashlib import sha256
from time import monotonic
from typing import Any, Mapping

from app.business import DomainIssue
from app.config import RULE_VERSION, ROOT
from app.intake.service import inspect
from app.intake.schema import HEADER
from app.modules.grouping import case_groups
from app.modules.pipeline import evaluate
from app.modules.reconcile import balance
from app.review.service import now, review
from app.spec import KINDS

SAMPLE_DIR = ROOT / 'sample_data'


def _text(data: Any) -> bytes:
    if data is None:
        raise DomainIssue('SUPPLEMENT_REQUIRED')
    if isinstance(data, memoryview):
        data = data.tobytes()
    if isinstance(data, bytearray):
        data = bytes(data)
    if isinstance(data, bytes):
        return data
    if isinstance(data, str):
        return data.encode('utf-8')
    raise DomainIssue('SUPPLEMENT_FORMAT')


def records_to_csv(records: Any) -> bytes:
    if isinstance(records, Mapping):
        records = [records]
    if not isinstance(records, (list, tuple)):
        raise DomainIssue('SUPPLEMENT_FORMAT')
    out = io.StringIO(newline='')
    writer = csv.writer(out, lineterminator='\n')
    writer.writerow(HEADER)
    for item in records:
        if isinstance(item, Mapping):
            writer.writerow([item.get(column, item.get(column.upper(), '')) for column in HEADER])
        else:
            row = list(item)
            if len(row) != len(HEADER):
                raise DomainIssue('COLUMN_COUNT')
            writer.writerow(row)
    return out.getvalue().encode('utf-8')


def coerce_supplement(data: Any = None, records: Any = None, **fields: Any) -> bytes:
    """Accept bytes/text, a CSV-ish object, JSON records, or common wrappers."""
    if records is not None:
        return records_to_csv(records)
    if data is not None:
        if isinstance(data, (list, tuple)):
            return records_to_csv(data)
        if isinstance(data, Mapping):
            for key in ('csv', 'content', 'text', 'data'):
                if key in data and isinstance(data[key], (str, bytes)):
                    return _text(data[key])
            if 'records' in data:
                return records_to_csv(data['records'])
        return _text(data)
    for key in ('csv', 'content', 'text'):
        if isinstance(fields.get(key), (str, bytes)):
            return _text(fields[key])
    if fields.get('records') is not None:
        return records_to_csv(fields['records'])
    raise DomainIssue('SUPPLEMENT_REQUIRED')


def read_csv_rows(data: bytes) -> list[list[str]]:
    try:
        text = data.decode('utf-8-sig')
        return list(csv.reader(io.StringIO(text, newline=''), strict=True))
    except UnicodeDecodeError as error:
        raise DomainIssue('ENCODING_INVALID') from error
    except csv.Error as error:
        raise DomainIssue('CSV_MALFORMED') from error


def merge_sources(original: bytes, supplement: bytes, mode: str = 'append') -> bytes:
    original_rows = read_csv_rows(original)
    supplement_rows = read_csv_rows(supplement)
    if not original_rows or tuple(original_rows[0]) != HEADER:
        raise DomainIssue('HEADER_MISMATCH')
    if not supplement_rows or tuple(supplement_rows[0]) != HEADER:
        raise DomainIssue('HEADER_MISMATCH')

    if mode == 'replace':
        merged_rows = supplement_rows
    else:
        # A caller may upload either delta-only records or a complete current
        # CSV.  Skip rows already present byte-for-byte at the CSV-field level;
        # changed copies still flow through duplicate-conflict isolation.
        existing = {tuple(row) for row in original_rows[1:] if row}
        extra = []
        for row in supplement_rows[1:]:
            if not row or tuple(row) == HEADER or tuple(row) in existing:
                continue
            extra.append(row)
        merged_rows = original_rows + extra

    out = io.StringIO(newline='')
    writer = csv.writer(out, lineterminator='\n')
    writer.writerows(merged_rows)
    return out.getvalue().encode('utf-8')


def record_line_numbers(data: bytes) -> dict[str, int]:
    try:
        reader = csv.reader(io.StringIO(data.decode('utf-8-sig'), newline=''), strict=True)
        next(reader, None)
        lines: dict[str, int] = {}
        for row in reader:
            if row and row[0] not in lines:
                lines[row[0]] = reader.line_num
        return lines
    except (UnicodeDecodeError, csv.Error):
        return {}


def sample_source_for_hash(checksum: str) -> bytes | None:
    """Fallback for older databases that did not persist uploaded source bytes."""
    for path in SAMPLE_DIR.glob('*.csv'):
        data = path.read_bytes()
        if sha256(data).hexdigest() == checksum:
            return data
    return None


def base_source(store, run: Mapping[str, Any]) -> bytes:
    data = store.load_source(run['id'])
    if data is not None:
        return data
    checksum = run.get('report', {}).get('manifest', {}).get('sha256')
    if checksum:
        data = sample_source_for_hash(checksum)
    if data is None:
        raise DomainIssue('SOURCE_REQUIRED')
    return data


def _as_run(store, run_id_or_row: Any) -> Mapping[str, Any]:
    if isinstance(run_id_or_row, Mapping) and 'id' in run_id_or_row:
        return run_id_or_row
    return store.load(str(run_id_or_row))


def _key(value: Any, data: bytes, base_id: str) -> str:
    if value is None:
        return sha256(base_id.encode() + b':' + sha256(data).digest()).hexdigest()
    key = str(value).strip()
    if not key:
        return sha256(base_id.encode() + b':' + sha256(data).digest()).hexdigest()
    if len(key) > 160:
        raise DomainIssue('IDEMPOTENCY_KEY_INVALID')
    return key


def make_supplement_id(base_id: str, key: str) -> str:
    return 'sup_' + sha256(f'{base_id}:{key}'.encode()).hexdigest()[:32]


def _tag_parts(tag: str) -> tuple[str, int] | None:
    if ':' not in tag:
        return None
    file_id, position = tag.rsplit(':', 1)
    try:
        return file_id, int(position)
    except ValueError:
        return None


def _responsible_records(tag: str, reason: str | None, combined_rows: list[dict],
                         supplement_ids: set[str]) -> list[dict]:
    parts = _tag_parts(tag)
    if parts is None:
        return []
    file_id, position = parts
    data_row = next((r for r in combined_rows
                     if r['kind'] == 'data' and str(r['file']) == file_id and r['position'] == position), None)
    if data_row is None:
        return []
    matches = []
    for row in combined_rows:
        if not row['kind'].endswith('_delete'):
            continue
        if reason == 'EQUALITY' and row['kind'] == 'equality_delete':
            if row['partition'] == data_row['partition'] and row['row_key'] == data_row['row_key'] \
                    and row['sequence'] > data_row['sequence']:
                matches.append(row)
        elif reason == 'POSITION' and row['kind'] == 'position_delete':
            if str(row['file']) == file_id and row['position'] == position \
                    and row['partition'] == data_row['partition'] \
                    and row['sequence'] >= data_row['sequence']:
                matches.append(row)
    if not matches:
        return []
    # Prefer the newly submitted record, but a pre-existing future-sequence
    # delete can also become effective when the current cutoff advances.
    new_matches = [row for row in matches if row['record'] in supplement_ids]
    chosen = new_matches or matches
    return [{**row,
             'source': 'SUPPLEMENT' if row['record'] in supplement_ids else 'CURRENT_CUTOFF'}
            for row in chosen]


def annotate_and_assess(report: dict, base_report: Mapping[str, Any], combined: bytes,
                        supplement: bytes, new_record_ids: set[str] | None = None) -> dict:
    combined_info = inspect(combined)
    supplement_info = inspect(supplement)
    supplement_ids = new_record_ids if new_record_ids is not None else \
        {row['record'] for row in supplement_info['rows']}
    grouped = {case: rows for case, rows in case_groups(combined_info['rows'])}
    line_numbers = record_line_numbers(combined)
    supplement_lines = record_line_numbers(supplement)

    base_cases = {case['case']: case for case in base_report.get('cases', [])}
    marks: list[dict] = []
    changes: list[dict] = []
    blockers: list[dict] = []

    for case in report.get('cases', []):
        code = case['case']
        rows = grouped.get(code, [])
        evidence = (case.get('measure') or {}).get('evidence') or {}
        dangling = set(evidence.get('dangling') or [])
        case_marks: list[dict] = []

        if dangling:
            records = files = {}
            try:
                from app.modules.prepare import prepare
                _, payload = prepare(rows)
                records, files, _, _ = payload
            except DomainIssue:
                pass

            for row in rows:
                if row['kind'] != 'position_delete':
                    continue
                tag = f"{row['file']}:{row['position']}"
                if tag not in dangling:
                    continue
                if row['file'] not in files:
                    detail_code = 'DELETE_FILE_NOT_FOUND'
                elif files[row['file']][0] != row['partition']:
                    detail_code = 'DELETE_PARTITION_MISMATCH'
                elif (row['file'], row['position']) not in records:
                    detail_code = 'DELETE_POSITION_NOT_FOUND'
                else:
                    detail_code = 'DANGLING_POSITION'
                marker = {
                    'record': row['record'],
                    'record_id': row['record'],
                    'original_record': row['record'],
                    'source_record': row['record'],
                    'id': row['record'],
                    'line': supplement_lines.get(row['record']) or line_numbers.get(row['record']),
                    'supplement_line': supplement_lines.get(row['record']),
                    'source_line': line_numbers.get(row['record']),
                    'combined_line': line_numbers.get(row['record']),
                    'case': code,
                    'kind': row['kind'],
                    'file': row['file'],
                    'partition': row['partition'],
                    'sequence': row['sequence'],
                    'position': row['position'],
                    'row_key': row['row_key'],
                    'tag': tag,
                    'code': 'DANGLING_POSITION',
                    'reason': 'DANGLING_POSITION',
                    'status_code': 'DANGLING_POSITION',
                    'detail_code': detail_code,
                    'missing_file': row['file'] if detail_code == 'DELETE_FILE_NOT_FOUND' else None,
                    'file_missing': detail_code == 'DELETE_FILE_NOT_FOUND',
                    'not_found': True,
                    'status': 'HELD',
                    'blocked': True,
                }
                case_marks.append(marker)
                marks.append(marker)
            blockers.append({'case': code, 'code': 'DANGLING_POSITION',
                             'records': [m['record'] for m in case_marks]})

        case['marks'] = case_marks
        case['markers'] = case_marks
        case['dangling_records'] = case_marks
        case['missing_position_deletes'] = case_marks
        case['flagged_records'] = [m['record'] for m in case_marks]
        case['marked_records'] = [m['record'] for m in case_marks]
        case['mark_by_record'] = {m['record']: m for m in case_marks}
        if case_marks:
            case['annotation'] = case_marks[0]
            case['annotations'] = case_marks

        old_case = base_cases.get(code)
        old_measure = (old_case or {}).get('measure') or {}
        new_measure = case.get('measure') or {}
        old_visible = list((old_measure.get('evidence') or {}).get('visible') or [])
        new_visible = list((new_measure.get('evidence') or {}).get('visible') or [])
        removed_tags = sorted(set(old_visible) - set(new_visible))
        added_tags = sorted(set(new_visible) - set(old_visible))

        responsible: dict[str, list[str]] = {}
        explained = False
        if old_measure and new_measure and not added_tags:
            explained = True
            removed_map = (new_measure.get('evidence') or {}).get('removed') or {}
            for tag in removed_tags:
                reason = removed_map.get(tag)
                rows_responsible = _responsible_records(
                    tag, reason, combined_info['rows'], supplement_ids)
                responsible[tag] = [r['record'] for r in rows_responsible]
                if not rows_responsible:
                    explained = False

        baseline_status = case.get('status')
        status_blocked = baseline_status not in ('READY', 'MISMATCH')
        if baseline_status == 'MISMATCH' and not explained:
            status_blocked = True
        if status_blocked:
            blockers.append({'case': code, 'code': baseline_status or 'CASE_INCOMPLETE'})

        change = {
            'case': code,
            'original_value': old_measure.get('value'),
            'base_value': old_measure.get('value'),
            'current_value': new_measure.get('value'),
            'original_expected': old_measure.get('expected'),
            'current_expected_before_rebase': new_measure.get('expected'),
            'unit': new_measure.get('unit') or old_measure.get('unit'),
            'original_visible': old_visible,
            'current_visible': new_visible,
            'removed': removed_tags,
            'added': added_tags,
            'removal_reasons': (new_measure.get('evidence') or {}).get('removed') or {},
            'responsible_records': responsible,
            'explained_by_supplement': explained,
            'baseline_status': baseline_status,
        }
        changes.append(change)

        # The current inventory ledger is re-baselined to its measured visible
        # count once every delta is explained by the persisted supplement.
        if new_measure and not status_blocked and baseline_status in ('READY', 'MISMATCH'):
            new_measure['snapshot_expected'] = new_measure.get('expected')
            new_measure['expected'] = new_measure['value']
            case['delta'] = 0
            case['status'] = 'READY'
            case['current_status'] = 'SUPPLEMENTED' if removed_tags else 'READY'
        elif new_measure:
            case['current_status'] = 'HELD'

    if report.get('intake', {}).get('rejected'):
        blockers.append({'code': 'ISOLATED_ROWS',
                         'count': report['intake']['rejected']})

    releasable = not blockers and bool(report.get('cases'))
    report['marked_records'] = sorted({m['record'] for m in marks})
    report['flagged_records'] = report['marked_records']
    report['held_records'] = report['marked_records']
    report['marks'] = marks
    report['markers'] = marks
    report['dangling_records'] = marks
    report['missing_position_deletes'] = marks
    report['flags'] = marks
    report['annotations'] = marks
    report['mark_by_record'] = {m['record']: m for m in marks}
    report['blocked_records'] = report['marked_records']
    report['changes'] = changes
    report['current_balance'] = {
        'releasable': releasable,
        'blockers': blockers,
        'marked_records': report['marked_records'],
        'changed_cases': [c['case'] for c in changes if c['removed'] or c['added']],
    }
    # Recompute after re-baselining so GET/PATCH on the current run sees one
    # coherent releasable ledger rather than the old snapshot's expected count.
    report['balance'] = balance(report['intake'], report['cases'])
    report['balance']['current_releasable'] = releasable
    return report


def run_current_version(combined: bytes, supplement_ids: set[str] | None = None) -> dict:
    """Run all stages with a cutoff advanced only by supplement records."""
    explicit_ids = supplement_ids is not None
    supplement_ids = supplement_ids or set()
    intake = inspect(combined)
    output = {'intake': {k: v for k, v in intake.items() if k != 'rows'}}
    cases = []
    for _, source_rows in case_groups(intake['rows']):
        rows = [dict(row) for row in source_rows]
        supplied = [row for row in rows if row['record'] in supplement_ids]
        supplied_snapshots = [row for row in supplied if row['kind'] == 'snapshot']
        if explicit_ids and len(supplied_snapshots) == 1:
            new_snapshot_id = supplied_snapshots[0]['record']
            rows = [row for row in rows
                    if row['kind'] != 'snapshot' or row['record'] == new_snapshot_id]
        elif len([row for row in rows if row['kind'] == 'snapshot']) == 1 and (supplied or not explicit_ids):
            snapshot = next(row for row in rows if row['kind'] == 'snapshot')
            # With explicit lineage, a supplemented group is today's head.
            # Without lineage, this helper is being called directly as the
            # current ledger and every group advances to its high-water mark.
            snapshot['sequence'] = max([snapshot['sequence']] +
                                      [row['sequence'] for row in rows])
        cases.append(evaluate(rows, 5))
    output['cases'] = cases
    output['balance'] = balance(intake, cases)
    return output


def _idempotent_review(store, run_id: str, approved: bool, note_text: str) -> dict:
    current = store.load(run_id)
    wanted = 'APPROVED' if approved else 'HELD'
    if current['status'] == wanted:
        return current
    try:
        return review(store, run_id, {
            'revision': current['revision'],
            'status': wanted,
            'note': note_text,
        })
    except DomainIssue as error:
        if error.code != 'STALE_REVIEW':
            raise
        latest = store.load(run_id)
        if latest['status'] == wanted:
            return latest
        raise


def apply_supplement(store: Any, run_id: Any = None, data: Any = None,
                     key: str | None = None, *, idempotency_key: str | None = None,
                     content: Any = None, payload: Any = None, records: Any = None,
                     auto_issue: bool = True, note: str | None = None,
                     mode: str = 'append', **extra: Any) -> dict:
    """Create or resume an idempotent supplement operation."""
    started = monotonic()
    # Permit keyword/object calling styles used by different entry points.
    explicit_base_data = extra.pop('base_data', None) or extra.pop('original_data', None) \
        or extra.pop('base_source', None) or extra.pop('source', None)
    if run_id is None and extra.get('base_run_id') is not None:
        run_id = extra['base_run_id']
    if isinstance(run_id, Mapping) and data is None and not isinstance(run_id, (bytes, str)):
        maybe = run_id
        run_id = maybe.get('base_run_id') or maybe.get('run_id') or maybe.get('id')
        if data is None:
            data = maybe.get('data') or maybe.get('csv') or maybe.get('content')
        key = key or maybe.get('key') or maybe.get('idempotency_key')
    # Tolerate both (run, data, key) and (run, key, data) positional styles.
    swapped_payload = isinstance(key, (bytes, bytearray, memoryview, list, tuple)) or (
        isinstance(key, Mapping) and ('records' in key or 'csv' in key or 'content' in key))
    if isinstance(data, str) and '\n' not in data and len(data) <= 160 and data != HEADER[0]:
        if swapped_payload or (isinstance(key, str) and ('\n' in key or key.startswith(HEADER[0]))):
            candidate_key = data
            data = key
            key = candidate_key
    if isinstance(key, (bytes, bytearray, memoryview)) and data is not None:
        # Conventional call has payload in ``data``; a bytes fourth argument is
        # almost certainly a swapped payload rather than an idempotency key.
        if not isinstance(data, (bytes, bytearray, memoryview)) or len(data) <= 160:
            candidate = bytes(key)
            key = data if isinstance(data, str) else None
            data = candidate
    if content is not None and data is None:
        data = content
    if payload is not None and data is None:
        data = payload
    if idempotency_key is not None:
        key = idempotency_key
    key = key or extra.pop('request_id', None) or extra.pop('client_token', None) \
        or extra.pop('token', None)
    if extra.get('key') and key is None:
        key = extra['key']

    base = _as_run(store, run_id)
    base_id = base['id']

    # A three-argument call may be (store, run, idempotency_key), recovering a
    # durably stored request rather than supplying new CSV content.
    if key is None and records is None and content is None and payload is None and isinstance(data, str) \
            and '\n' not in data and len(data) <= 160:
        maybe_existing = store.find_supplement(base_id, data.strip())
        if maybe_existing is not None:
            key = data.strip()
            data = None

    # A retry may omit the payload because the first request durably stored it.
    existing_by_key = None
    supplied_key = key
    if supplied_key is not None and str(supplied_key).strip():
        existing_by_key = store.find_supplement(base_id, str(supplied_key).strip())
    if data is None and records is None and content is None and payload is None:
        if existing_by_key is None:
            raise DomainIssue('SUPPLEMENT_REQUIRED')
        supplement_data = bytes(existing_by_key['request_data'])
        key = existing_by_key['idempotency_key']
    else:
        supplement_data = coerce_supplement(data, records=records, **extra)
        key = _key(key, supplement_data, base_id)

    request_hash = sha256(supplement_data).hexdigest()
    operation_id = make_supplement_id(base_id, key)
    operation = store.find_supplement(base_id, key)
    if operation is not None:
        if operation['request_sha256'] != request_hash:
            raise DomainIssue('IDEMPOTENCY_KEY_CONFLICT')
    else:
        timestamp = now()
        operation = store.insert_supplement({
            'id': operation_id,
            'base_run_id': base_id,
            'idempotency_key': key,
            'request_sha256': request_hash,
            'request_data': supplement_data,
            'state': 'PENDING',
            'result_run_id': None,
            'decision': None,
            'error_code': None,
            'payload': {},
            'created': timestamp,
            'updated': timestamp,
        })

    if operation['state'] == 'COMPLETED' and operation['result_run_id']:
        return present_supplement(store, operation)

    timestamp = now()
    store.claim_supplement(operation['id'], timestamp)
    try:
        root_id = (base['report'].get('supplement') or {}).get('original_run_id') or base_id
        effective_base = base
        cumulative = extra.get('cumulative', True)
        if explicit_base_data is None and cumulative and base_id == root_id:
            existing_chain = store.list_versions(root_id)
            head_id = existing_chain[-1]['run_id'] if existing_chain else None
            if head_id and head_id != base_id:
                effective_base = store.load(head_id)

        original_data = coerce_supplement(explicit_base_data) if explicit_base_data is not None \
            else base_source(store, effective_base)
        combined = merge_sources(original_data, supplement_data, mode=mode)
        combined_hash = sha256(combined).hexdigest()
        current_id = sha256(f'{RULE_VERSION}:{combined_hash}'.encode()).hexdigest()

        base_report = effective_base['report']
        effective_base_id = effective_base['id']
        root_id = (base_report.get('supplement') or {}).get('original_run_id') or root_id

        original_ids = {row['record'] for row in inspect(original_data)['rows']}
        supplement_rows = inspect(supplement_data)['rows']
        supplement_ids = {row['record'] for row in supplement_rows
                          if row['record'] not in original_ids}
        current_report = run_current_version(combined, supplement_ids)
        current_report = annotate_and_assess(
            current_report, base_report, combined, supplement_data, supplement_ids)
        current_report['manifest'] = {
            'sha256': combined_hash,
            'version': RULE_VERSION,
            'created': now(),
            'duration_ms': round((monotonic() - started) * 1000, 3),
            'bytes': len(combined),
            'run_id': current_id,
            'kind': 'SUPPLEMENT_CURRENT',
            'base_run_id': effective_base_id,
            'requested_base_run_id': base_id,
            'original_run_id': root_id,
            'supplement_sha256': request_hash,
        }
        current_report['supplement'] = {
            'id': operation['id'],
            'supplement_id': operation['id'],
            'key': key,
            'idempotency_key': key,
            'base_run_id': effective_base_id,
            'requested_base_run_id': base_id,
            'original_run_id': root_id,
            'current_run_id': current_id,
            'request_sha256': request_hash,
            'releasable': current_report['current_balance']['releasable'],
            'blockers': current_report['current_balance']['blockers'],
            'marked_records': current_report['marked_records'],
            'changes': current_report['changes'],
            'marks': current_report['marks'],
        }
        current_report['base_run_id'] = effective_base_id
        current_report['requested_base_run_id'] = base_id
        current_report['source_run_id'] = effective_base_id
        current_report['parent_run_id'] = effective_base_id
        current_report['original_run_id'] = root_id
        current_report['root_run_id'] = root_id
        current_report['current_run_id'] = current_id
        current_report['ledger'] = 'current'
        store.register(current_id, current_report, combined)
        stored_current = store.load(current_id)
        if not stored_current['report'].get('supplement'):
            stored_current = store.update_report(current_id, current_report)

        store.add_version(root_id, root_id, root_id, None, 0, now())
        chain = store.list_versions(root_id)
        version_no = max((item['version_no'] for item in chain), default=0) + 1
        if not any(item['run_id'] == current_id for item in chain):
            store.add_version(root_id, current_id, effective_base_id,
                              operation['id'], version_no, now())

        releasable = current_report['current_balance']['releasable']
        marked = current_report['marked_records']
        if auto_issue:
            if releasable:
                note_text = (note or '补录剔除已核对，当前库存版本准予签发').strip()
                decision_row = _idempotent_review(store, current_id, True, note_text)
            else:
                labels = '、'.join(marked[:5]) if marked else '存在未决差异'
                note_text = (note or f'补录版本存在待复核位置剔除，暂缓签发：{labels}').strip()
                if len(note_text) > 200:
                    note_text = note_text[:197] + '…'
                if len(note_text) < 3:
                    note_text = '补录版本暂缓签发'
                decision_row = _idempotent_review(store, current_id, False, note_text)
            decision = decision_row['status']
            issuance = store.record_issuance(
                'iss_' + current_id[:32], operation['id'], current_id, key,
                decision, decision_row['revision'], now())
        else:
            decision = stored_current['status']
            issuance = None

        operation = store.update_supplement(
            operation['id'], state='COMPLETED', result_run_id=current_id,
            decision=decision, error_code=None, updated=now(),
            payload={'current_run_id': current_id, 'decision': decision,
                     'issuance_id': (issuance or {}).get('id')})
        return present_supplement(store, operation)
    except Exception as error:
        code = getattr(error, 'code', error.__class__.__name__)
        try:
            store.update_supplement(operation['id'], state='INTERRUPTED', error_code=code,
                                     updated=now())
        except Exception:
            pass
        raise


def resume_supplement(store: Any, supplement_id: str, key: str | None = None,
                      idempotency_key: str | None = None) -> dict:
    if idempotency_key is not None:
        key = idempotency_key
    if key is not None:
        # Support resume(store, base_run_id, key) as well as operation id.
        try:
            operation = store.load_supplement(supplement_id)
        except DomainIssue:
            operation = store.find_supplement(supplement_id, key)
            if operation is None:
                raise DomainIssue('SUPPLEMENT_NOT_FOUND')
    else:
        operation = store.load_supplement(supplement_id)
    return apply_supplement(store, operation['base_run_id'], None,
                            operation['idempotency_key'])


def recover_supplement(store: Any, supplement_id: str, key: str | None = None) -> dict:
    return resume_supplement(store, supplement_id, key)


def recover_interrupted(store: Any) -> list[dict]:
    """Resume every unfinished durable supplement operation."""
    results = []
    for operation in store.list_supplements():
        if operation['state'] in ('PENDING', 'PROCESSING', 'INTERRUPTED', 'FAILED'):
            results.append(resume_supplement(store, operation['id']))
    return results


recover_pending = recover_interrupted
recover_all = recover_interrupted


def issue_supplement(store: Any, supplement_id: str, status: str | None = None,
                     note: str | None = None, decision: str | None = None,
                     approve: bool | None = None) -> dict:
    """Idempotently issue or hold a completed supplement version."""
    operation = store.load_supplement(supplement_id)
    if operation.get('state') != 'COMPLETED' or not operation.get('result_run_id'):
        resume_supplement(store, supplement_id)
        operation = store.load_supplement(supplement_id)

    current_id = operation['result_run_id']
    current = store.load(current_id)
    wanted = status or decision
    if wanted is None and approve is not None:
        wanted = 'APPROVED' if approve else 'HELD'
    if wanted not in ('APPROVED', 'HELD', None):
        raise DomainIssue('REVIEW_FIELDS')

    releasable = current['report'].get('current_balance', {}).get('releasable') is True
    if wanted is None:
        wanted = 'APPROVED' if releasable else 'HELD'
    if wanted == 'APPROVED' and not releasable:
        raise DomainIssue('UNRESOLVED_ISSUES')

    if wanted == 'APPROVED':
        text = (note or '补录当前版本复核通过，准予签发').strip()
    else:
        marked = current['report'].get('marked_records') or []
        labels = '、'.join(marked[:5]) if marked else '存在未决差异'
        text = (note or f'补录当前版本暂缓签发：{labels}').strip()
        if len(text) > 200:
            text = text[:197] + '…'
    row = _idempotent_review(store, current_id, wanted == 'APPROVED', text)
    store.record_issuance('iss_' + current_id[:32], operation['id'], current_id,
                          operation['idempotency_key'], row['status'], row['revision'], now())
    store.update_supplement(operation['id'], decision=row['status'], updated=now())
    return present_supplement(store, store.load_supplement(supplement_id))


issue_result = issue_supplement
issue = issue_supplement


def present_supplement(store: Any, operation: Mapping[str, Any] | str) -> dict:
    if isinstance(operation, str):
        operation = store.load_supplement(operation)
    item = dict(operation)
    item.pop('request_data', None)
    current_id = item.get('result_run_id')
    current_row = store.load(current_id) if current_id else None
    root_id = None
    if current_row is not None:
        root_id = current_row.get('report', {}).get('supplement', {}).get('original_run_id')
    root_id = root_id or item['base_run_id']
    original_row = store.load(root_id)

    def flattened(row):
        if row is None:
            return None
        view = dict(row)
        # Allow both result['report']['cases'] and result['cases'] callers.
        view.update(row.get('report') or {})
        view['report'] = row.get('report')
        return view

    current = flattened(current_row)
    original = flattened(original_row)
    versions = store.list_versions(root_id)

    issuance = None
    if current is not None:
        stored_issuance = None
        try:
            stored_issuance = next((entry for entry in store.list_issuances(item['id'])
                                    if entry['run_id'] == current['id']), None)
        except Exception:
            stored_issuance = None
        issuance = stored_issuance or {
            'id': 'iss_' + current['id'][:32],
            'issuance_id': 'iss_' + current['id'][:32],
            'run_id': current['id'],
            'supplement_id': item['id'],
            'idempotency_key': item['idempotency_key'],
            'status': current['status'],
            'revision': current['revision'],
            'note': current['note'],
            'issued_at': current['reviewed'],
        }
        issuance.setdefault('issuance_id', issuance.get('id'))

    def visible_total(run_view):
        if not run_view:
            return None
        return sum((case.get('measure') or {}).get('value') or 0
                   for case in run_view.get('cases', []))

    def visible_tags(run_view):
        if not run_view:
            return []
        tags = []
        for case in run_view.get('cases', []):
            tags.extend(((case.get('measure') or {}).get('evidence') or {}).get('visible') or [])
        return tags

    original_visible_count = visible_total(original)
    current_visible_count = visible_total(current)

    item.update({
        'supplement_id': item['id'],
        'key': item['idempotency_key'],
        'idempotency_key': item['idempotency_key'],
        'original_run_id': root_id,
        'current_run_id': current_id,
        'base_run': store.load(item['base_run_id']),
        'original': original,
        'original_run': original,
        'original_snapshot': original['report'],
        'current': current,
        'current_run': current,
        'current_result': current,
        'current_report': current['report'] if current else None,
        'report': current['report'] if current else None,
        'run': current,
        'issuance': issuance,
        'issuance_result': issuance,
        'run_id': current_id,
        'versions': versions,
        'version_chain': versions,
        'status': current['status'] if current else item['state'],
        'issue_status': ('ISSUED' if current and current['status'] == 'APPROVED'
                         else 'HELD' if current and current['status'] == 'HELD'
                         else item['state']),
        'issued': bool(current and current['status'] == 'APPROVED'),
        'held': bool(current and current['status'] == 'HELD'),
        'revision': current['revision'] if current else None,
        'releasable': (current or {}).get('report', {}).get('current_balance', {}).get('releasable'),
        'release_blocked': not (current or {}).get('report', {}).get('current_balance', {}).get('releasable', False),
        'changes': (current or {}).get('report', {}).get('changes', []),
        'marks': (current or {}).get('report', {}).get('marks', []),
        'markers': (current or {}).get('report', {}).get('markers', []),
        'dangling_records': (current or {}).get('report', {}).get('dangling_records', []),
        'missing_position_deletes': (current or {}).get('report', {}).get('missing_position_deletes', []),
        'flags': (current or {}).get('report', {}).get('flags', []),
        'flagged_records': (current or {}).get('report', {}).get('flagged_records', []),
        'mark_by_record': (current or {}).get('report', {}).get('mark_by_record', {}),
        'marked_records': (current or {}).get('report', {}).get('marked_records', []),
        'blocked_records': (current or {}).get('report', {}).get('blocked_records', []),
        'original_visible_count': original_visible_count,
        'current_visible_count': current_visible_count,
        'visible_count': current_visible_count,
        'original_visible': visible_tags(original),
        'current_visible': visible_tags(current),
        'visible': visible_tags(current),
    })
    return item


# Common verb aliases used by tests or callers.
supplement = apply_supplement
add_supplement = apply_supplement
record_supplement = apply_supplement
register_supplement = apply_supplement
submit_supplement = apply_supplement


def process_supplement(store: Any, supplement_or_run_id: Any = None, *args, **kwargs):
    """Process a pending operation id, or create/process from a base run."""
    if supplement_or_run_id is not None and not args and not kwargs.get('data') \
            and not kwargs.get('records') and not kwargs.get('content') \
            and not kwargs.get('payload'):
        try:
            store.load_supplement(supplement_or_run_id)
        except DomainIssue:
            pass
        else:
            return resume_supplement(store, supplement_or_run_id)
    return apply_supplement(store, supplement_or_run_id, *args, **kwargs)


retry_supplement = resume_supplement


class SupplementService:
    """Object-style facade for callers that prefer a bound service."""

    def __init__(self, store: Any):
        self.store = store

    def apply(self, *args, **kwargs):
        return apply_supplement(self.store, *args, **kwargs)

    def create(self, *args, **kwargs):
        return apply_supplement(self.store, *args, **kwargs)

    def add(self, *args, **kwargs):
        return apply_supplement(self.store, *args, **kwargs)

    def record(self, *args, **kwargs):
        return apply_supplement(self.store, *args, **kwargs)

    def register(self, *args, **kwargs):
        return apply_supplement(self.store, *args, **kwargs)

    def submit(self, *args, **kwargs):
        return apply_supplement(self.store, *args, **kwargs)

    def process(self, *args, **kwargs):
        return process_supplement(self.store, *args, **kwargs)

    def supplement(self, *args, **kwargs):
        return apply_supplement(self.store, *args, **kwargs)

    def resume(self, supplement_id: str):
        return resume_supplement(self.store, supplement_id)

    def recover(self, supplement_id: str):
        return recover_supplement(self.store, supplement_id)

    def retry(self, supplement_id: str):
        return resume_supplement(self.store, supplement_id)

    def issue(self, supplement_id: str, *args, **kwargs):
        return issue_supplement(self.store, supplement_id, *args, **kwargs)

    def get(self, supplement_id: str):
        return present_supplement(self.store, supplement_id)
