-- Build a user dimension from raw users + signup events.
CREATE TABLE dim_users AS
SELECT u.user_id, u.name, MIN(e.ts) AS first_seen
FROM raw_users u
LEFT JOIN raw_events e ON e.user_id = u.user_id
GROUP BY u.user_id, u.name;
