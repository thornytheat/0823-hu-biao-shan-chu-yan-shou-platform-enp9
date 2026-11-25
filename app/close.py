from app.supplement.service import apply_supplement, present_supplement

close_period = apply_supplement
month_end_close = apply_supplement


def close_result(store, supplement_id):
    return present_supplement(store, supplement_id)


__all__ = ['close_period', 'month_end_close', 'close_result']
