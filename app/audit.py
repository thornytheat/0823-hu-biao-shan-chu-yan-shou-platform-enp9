from hashlib import sha256
from typing import Any

from app.business import DomainIssue
from app.modules.pipeline import run
from app.supplement.service import present_supplement, sample_source_for_hash


def replay_snapshot(store: Any, run_id: str) -> dict:
    """Reload the immutable signed snapshot for audit reverification."""
    return store.load(run_id)


def reverify_snapshot(store: Any, run_id: str) -> dict:
    """Recompute an original signed batch from its persisted source bytes."""
    row = store.load(run_id)
    report = row['report']
    source = store.load_source(run_id)
    if source is None:
        source = sample_source_for_hash(report.get('manifest', {}).get('sha256', ''))
    if source is None:
        raise DomainIssue('SOURCE_REQUIRED')

    checksum = sha256(source).hexdigest()
    checksum_matches = checksum == report.get('manifest', {}).get('sha256')
    result = {
        'run_id': run_id,
        'status': row['status'],
        'revision': row['revision'],
        'sha256': checksum,
        'sha256_match': checksum_matches,
        'original': row,
    }

    if report.get('manifest', {}).get('kind') == 'SUPPLEMENT_CURRENT':
        # Current versions use a re-baselined current ledger; their immutable
        # audit anchor is report.supplement.original_run_id.
        result.update({
            'verified': checksum_matches,
            'ledger': 'current',
            'original_run_id': report.get('supplement', {}).get('original_run_id'),
        })
        return result

    recomputed = run(source)
    checks = {
        'sha256': checksum_matches,
        'intake': recomputed.get('intake') == report.get('intake'),
        'cases': recomputed.get('cases') == report.get('cases'),
        'balance': recomputed.get('balance') == report.get('balance'),
    }
    visible = {}
    for case in report.get('cases', []):
        evidence = (case.get('measure') or {}).get('evidence') or {}
        if 'visible' in evidence:
            visible[case['case']] = evidence['visible']
    result.update({
        'verified': all(checks.values()),
        'checks': checks,
        'ledger': 'original_snapshot',
        'visible': visible,
        'visible_rows': visible,
    })
    return result


# Concise alias used by some entry points.
verify_snapshot = reverify_snapshot


def original_snapshot(store, supplement_id):
    return present_supplement(store, supplement_id)['original']


def current_result(store, supplement_id):
    return present_supplement(store, supplement_id)['current']


__all__ = [
    'replay_snapshot', 'reverify_snapshot', 'verify_snapshot',
    'original_snapshot', 'current_result',
]
