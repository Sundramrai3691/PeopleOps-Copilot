-- Synthetic USD amounts in integer cents. Dates are ISO YYYY-MM-DD.
PRAGMA foreign_keys=ON;
CREATE TABLE customers(customer_id INTEGER PRIMARY KEY, name TEXT NOT NULL, region TEXT);
CREATE TABLE orders(order_id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers, ordered_at TEXT NOT NULL, status TEXT NOT NULL, gross_cents INTEGER NOT NULL);
CREATE TABLE refunds(refund_id INTEGER PRIMARY KEY, order_id INTEGER REFERENCES orders, refunded_at TEXT NOT NULL, amount_cents INTEGER NOT NULL);
INSERT INTO customers VALUES (1,'Ada','West'),(2,'Ben','East'),(3,'Cy',NULL),(4,'Di','West');
INSERT INTO orders VALUES
(101,1,'2026-01-01','paid',10000),(102,1,'2026-01-31','paid',5000),
(103,2,'2026-01-15','paid',8000),(104,2,'2026-01-20','cancelled',9000),
(105,3,'2026-01-25','paid',6000),(106,1,'2026-02-01','paid',7000);
INSERT INTO refunds VALUES
(1,101,'2026-01-05',1000),(2,101,'2026-02-02',2000),
(3,103,'2026-01-18',8000),(4,106,'2026-02-03',500);
