SELECT
  tags.`path`,
  tags.line,
  tags.kind,
  tags.name,
  tags.lname,
  tags.sort_by,
  tags.pattern,
  tags.typeref,
  tags.scope,
  tags.scopeKind,
  tags.`access`
FROM tags
JOIN files
ON files.filename = tags.`path`
WHERE
  X_NORMALIZE(:word) <> ''
  AND
  files.filetype = X_NORMALIZE(:filetype)
  AND
  tags.lname LIKE X_LIKE_ESC(X_LOWER(X_NORMALIZE(SUBSTR(:word, 1, :exact)))) ESCAPE '!'
  AND
  NOT INSTR(:word, tags.lname)
  AND
  X_SIMILARITY(X_LOWER(X_NORMALIZE(SUBSTR(:word, 1, :exact))), tags.lname) > :cut_off
LIMIT :limit
