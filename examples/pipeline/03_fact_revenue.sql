-- Join staged orders with the user dim into the reporting fact table.
INSERT INTO fact_revenue
SELECT d.user_id, d.name, SUM(o.amount) AS revenue
FROM stg_orders o
JOIN dim_users d ON o.user_id = d.user_id
GROUP BY d.user_id, d.name;
