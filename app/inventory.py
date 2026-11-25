from app.supplement.service import apply_supplement, present_supplement

apply_late_deletes = apply_supplement
apply_deletions = apply_supplement


def current_inventory(store, supplement_id):
    return present_supplement(store, supplement_id)['current']


__all__ = ['apply_late_deletes', 'apply_deletions', 'current_inventory']
