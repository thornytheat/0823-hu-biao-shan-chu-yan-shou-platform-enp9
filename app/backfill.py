from app.supplement.service import apply_supplement, resume_supplement

apply_backfill = apply_supplement
record_backfill = apply_supplement
run_backfill = apply_supplement
retry_backfill = resume_supplement

__all__ = ['apply_supplement', 'apply_backfill', 'record_backfill',
           'run_backfill', 'resume_supplement', 'retry_backfill']
