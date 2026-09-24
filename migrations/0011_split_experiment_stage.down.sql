-- The code before 0011 knows one experiment stage. Folding all three back into it is
-- lossy - which of them a run was on is not recoverable - and that is the honest cost
-- of rolling back: the old code offers the old, wider menu again.
UPDATE runs SET stage = 'experiment' WHERE stage IN ('draft', 'evaluation', 'rollout');
