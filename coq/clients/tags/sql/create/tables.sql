BEGIN;


CREATE TABLE IF NOT EXISTS files (
  filename TEXT NOT NULL PRIMARY KEY
) WITHOUT ROWID;


-- !! files 1:N tags
CREATE TABLE IF NOT EXISTS tags (
  filename  TEXT    NOT NULL REFERENCES files (filename) ON DELETE CASCADE,
  language  TEXT    NOT NULL,
  line      INTEGER,
  name      TEXT    NOT NULL,
  lname     TEXT    NOT NULL AS (X_LOWER(name)) STORED,
  pattern   TEXT    NOT NULL,
  roles     TEXT,
  kind      TEXT,
  typeref   TEXT,
  scope     TEXT,
  scopeKind TEXT,
  'access'  TEXT
);
CREATE INDEX IF NOT EXISTS tags_filename ON tags (filename);
CREATE INDEX IF NOT EXISTS tags_line_num ON tags (line_num);
CREATE INDEX IF NOT EXISTS tags_name     ON tags (name);
CREATE INDEX IF NOT EXISTS tags_lname    ON tags (lname);


END;
