from app.supplement.service import apply_supplement, resume_supplement

apply_correction = apply_supplement
record_correction = apply_supplement
create_correction = apply_supplement
retry_correction = resume_supplement

__all__ = ['apply_supplement', 'apply_correction', 'record_correction',
           'create_correction', 'resume_supplement', 'retry_correction']
