-- SPEC-registry.md, T22: one experiment stage becomes three.
--
-- `create_experiment_draft` and `roll_out_variant_to_percentage` were both declared on
-- `experiment`, so a turn writing a reversible draft was also offered the one
-- irreversible act. They now live on `draft` and `rollout`, with `evaluation` between.
--
-- A run left on `experiment` would be shown no tool at all, which looks exactly like a
-- run with nothing to do. Every such run was, at most, drafting - the rollout only ever
-- happened after a human's answer - so `draft` is the stage it was actually in.

UPDATE runs SET stage = 'draft' WHERE stage = 'experiment';
