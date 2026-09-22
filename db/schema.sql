-- ============================================================================
-- dt-demo-app database schema + seed data
-- Target: PostgreSQL 15, database `dtdemo`, application user `dtdemo`
-- Idempotent: safe to re-run, drops and recreates everything below.
-- ============================================================================

DROP TABLE IF EXISTS order_events CASCADE;
DROP TABLE IF EXISTS orders CASCADE;
DROP TABLE IF EXISTS inventory CASCADE;
DROP TABLE IF EXISTS products CASCADE;
DROP TABLE IF EXISTS customers CASCADE;

-- ----------------------------------------------------------------------------
-- Tables
-- ----------------------------------------------------------------------------

CREATE TABLE customers (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    tier        TEXT NOT NULL CHECK (tier IN ('standard', 'silver', 'gold')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE products (
    sku         TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    category    TEXT NOT NULL,
    base_price  NUMERIC(10, 2) NOT NULL
);

CREATE TABLE inventory (
    sku             TEXT PRIMARY KEY REFERENCES products(sku),
    qty_on_hand     INT NOT NULL,
    reorder_level   INT NOT NULL
);

CREATE TABLE orders (
    id           SERIAL PRIMARY KEY,
    customer_id  INT REFERENCES customers(id),
    sku          TEXT REFERENCES products(sku),
    qty          INT NOT NULL,
    unit_price   NUMERIC(10, 2) NOT NULL,
    total_price  NUMERIC(10, 2) NOT NULL,
    status       TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE order_events (
    id          SERIAL PRIMARY KEY,
    order_id    INT REFERENCES orders(id),
    event_type  TEXT NOT NULL,
    detail      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ----------------------------------------------------------------------------
-- Indexes (support the "recent orders" and "revenue by category" report
-- queries the Java tier runs; the revenue report is intentionally the
-- heaviest query in the app so it shows up as a slow DB span in Dynatrace).
-- ----------------------------------------------------------------------------

CREATE INDEX idx_orders_created_at ON orders(created_at);
CREATE INDEX idx_orders_customer_id ON orders(customer_id);
CREATE INDEX idx_order_events_order_id ON order_events(order_id);

-- ----------------------------------------------------------------------------
-- Seed: 25 customers spread across the three tiers
-- ----------------------------------------------------------------------------

INSERT INTO customers (name, tier) VALUES
    ('Alice Johnson',   'standard'),
    ('Bob Smith',       'standard'),
    ('Carol Williams',  'silver'),
    ('David Brown',     'gold'),
    ('Eva Davis',       'standard'),
    ('Frank Miller',    'silver'),
    ('Grace Wilson',    'gold'),
    ('Henry Moore',     'standard'),
    ('Ivy Taylor',      'silver'),
    ('Jack Anderson',   'standard'),
    ('Karen Thomas',    'gold'),
    ('Leo Jackson',     'standard'),
    ('Mia White',       'silver'),
    ('Noah Harris',     'gold'),
    ('Olivia Martin',   'standard'),
    ('Paul Thompson',   'silver'),
    ('Quinn Garcia',    'gold'),
    ('Rachel Martinez', 'standard'),
    ('Sam Robinson',    'silver'),
    ('Tina Clark',      'gold'),
    ('Uma Rodriguez',   'standard'),
    ('Victor Lewis',    'silver'),
    ('Wendy Lee',       'gold'),
    ('Xavier Walker',   'standard'),
    ('Yara Hall',       'silver');

-- ----------------------------------------------------------------------------
-- Seed: 40 products across 5 categories (8 per category), varied prices
-- ----------------------------------------------------------------------------

INSERT INTO products (sku, name, category, base_price) VALUES
    -- electronics
    ('ELEC-001', 'Wireless Mouse',              'electronics', 24.99),
    ('ELEC-002', 'Bluetooth Speaker',            'electronics', 49.99),
    ('ELEC-003', '4K Monitor',                   'electronics', 329.99),
    ('ELEC-004', 'USB-C Hub',                    'electronics', 39.99),
    ('ELEC-005', 'Mechanical Keyboard',          'electronics', 89.99),
    ('ELEC-006', 'Noise Cancelling Headphones',  'electronics', 199.99),
    ('ELEC-007', 'Smartwatch',                   'electronics', 249.99),
    ('ELEC-008', 'Portable SSD 1TB',             'electronics', 119.99),
    -- apparel
    ('APP-001', 'Men''s Running Shoes',          'apparel', 79.99),
    ('APP-002', 'Women''s Yoga Pants',           'apparel', 44.99),
    ('APP-003', 'Cotton T-Shirt',                'apparel', 19.99),
    ('APP-004', 'Denim Jacket',                  'apparel', 69.99),
    ('APP-005', 'Wool Sweater',                  'apparel', 59.99),
    ('APP-006', 'Rain Jacket',                   'apparel', 89.99),
    ('APP-007', 'Athletic Socks 3-Pack',         'apparel', 12.99),
    ('APP-008', 'Baseball Cap',                  'apparel', 22.99),
    -- home
    ('HOME-001', 'Stainless Steel Cookware Set', 'home', 149.99),
    ('HOME-002', 'Memory Foam Pillow',           'home', 34.99),
    ('HOME-003', 'LED Desk Lamp',                'home', 27.99),
    ('HOME-004', 'Air Purifier',                 'home', 179.99),
    ('HOME-005', 'Cotton Bath Towel Set',        'home', 39.99),
    ('HOME-006', 'Ceramic Dinnerware Set',       'home', 89.99),
    ('HOME-007', 'Robot Vacuum',                 'home', 299.99),
    ('HOME-008', 'Throw Blanket',                'home', 24.99),
    -- grocery
    ('GRO-001', 'Organic Coffee Beans',          'grocery', 14.99),
    ('GRO-002', 'Extra Virgin Olive Oil',        'grocery', 16.99),
    ('GRO-003', 'Almond Butter',                 'grocery', 9.99),
    ('GRO-004', 'Sparkling Water 12-Pack',       'grocery', 8.99),
    ('GRO-005', 'Granola Bars Box',              'grocery', 6.99),
    ('GRO-006', 'Green Tea 24ct',                'grocery', 7.99),
    ('GRO-007', 'Dark Chocolate Bar',            'grocery', 4.99),
    ('GRO-008', 'Trail Mix 1lb',                 'grocery', 8.49),
    -- sports
    ('SPT-001', 'Yoga Mat',                      'sports', 29.99),
    ('SPT-002', 'Adjustable Dumbbell Set',       'sports', 149.99),
    ('SPT-003', 'Basketball',                    'sports', 24.99),
    ('SPT-004', 'Camping Tent 4-Person',         'sports', 189.99),
    ('SPT-005', 'Insulated Water Bottle',        'sports', 19.99),
    ('SPT-006', 'Resistance Bands Set',          'sports', 22.99),
    ('SPT-007', 'Cycling Helmet',                'sports', 59.99),
    ('SPT-008', 'Foam Roller',                   'sports', 27.99);

-- ----------------------------------------------------------------------------
-- Seed: inventory for every SKU, qty_on_hand between 50 and 5000
-- ----------------------------------------------------------------------------

INSERT INTO inventory (sku, qty_on_hand, reorder_level)
SELECT
    sku,
    (50 + floor(random() * 4951))::int   AS qty_on_hand,
    (20 + floor(random() * 181))::int    AS reorder_level
FROM products;

-- ----------------------------------------------------------------------------
-- Grants for the application user
-- ----------------------------------------------------------------------------

GRANT ALL ON ALL TABLES IN SCHEMA public TO dtdemo;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO dtdemo;
