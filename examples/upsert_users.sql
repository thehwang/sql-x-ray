-- Synthetic example: a MERGE upsert followed by housekeeping UPDATEs.
-- Demonstrates SQL X-Ray's write-statement data flow (UPDATE / MERGE).

MERGE dim_users AS T
USING (
  SELECT id, name, email
  FROM (
    SELECT id, name, email FROM stg_users_a
    UNION ALL
    SELECT id, name, email FROM stg_users_b
  )
  WHERE email IS NOT NULL
) AS S
ON T.id = S.id
WHEN MATCHED THEN
  UPDATE SET name = S.name, email = S.email, updated_at = CURRENT_TIMESTAMP()
WHEN NOT MATCHED THEN
  INSERT (id, name, email, active)
  VALUES (S.id, S.name, S.email, true);

-- Deactivate users that haven't logged in for a year (scoped UPDATE ... FROM).
UPDATE dim_users T
SET T.active = false
FROM stale_logins S
WHERE T.id = S.user_id;
