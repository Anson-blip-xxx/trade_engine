-- Only a fair iteration hint. Never authorization, an order or financial state.
CREATE TABLE IF NOT EXISTS v2_strategy_dispatch_cursors (
    consumer text PRIMARY KEY,
    order_id text NOT NULL
);
