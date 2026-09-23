ALTER TABLE approvals DROP CONSTRAINT grants_name_their_source;
ALTER TABLE approvals DROP CONSTRAINT approval_grant_kind_is_known;
ALTER TABLE approvals DROP COLUMN rule;
ALTER TABLE approvals DROP COLUMN granted_by;
