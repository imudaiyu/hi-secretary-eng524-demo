ALTER TABLE core.demo DROP COLUMN old_col;

LOCK TABLE core.demo IN SHARE MODE;
UPDATE core.demo SET col = 'x';
CREATE INDEX idx_demo ON core.demo (col);
