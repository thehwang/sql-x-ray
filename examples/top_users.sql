WITH active AS (
  SELECT user_id, MAX(login_at) AS last_login
  FROM events WHERE event_type = 'login' GROUP BY user_id
),
paid AS (
  SELECT user_id, SUM(amount) AS total
  FROM orders WHERE status = 'paid' GROUP BY user_id HAVING SUM(amount) > 100
),
ranked AS (
  SELECT p.user_id, p.total,
         ROW_NUMBER() OVER (PARTITION BY u.country ORDER BY p.total DESC) AS rn
  FROM paid p JOIN users u ON p.user_id = u.id
)
SELECT r.user_id, r.total, a.last_login
FROM ranked r LEFT JOIN active a ON r.user_id = a.user_id
WHERE r.rn <= 10;
