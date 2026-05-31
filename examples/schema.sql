-- Synthetic schema for the example queries. Feed via --schema so SELECT *
-- expands into real columns and lineage resolves precisely.

CREATE TABLE users (
  user_id    INT64,
  name       STRING,
  email      STRING,
  created_at TIMESTAMP
);

CREATE TABLE orders (
  order_id INT64,
  user_id  INT64,
  amount   FLOAT64,
  ts       TIMESTAMP
);

CREATE TABLE events (
  user_id  INT64,
  login_at TIMESTAMP,
  country  STRING
);
