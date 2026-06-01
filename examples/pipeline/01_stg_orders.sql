-- Stage raw orders into a cleaned staging table.
CREATE TABLE stg_orders AS
SELECT order_id, user_id, amount, status
FROM raw_orders
WHERE status = 'paid';
