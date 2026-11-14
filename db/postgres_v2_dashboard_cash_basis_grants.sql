-- Read-only proof fields required by the dashboard's cash-basis classifier.
-- No table-wide SELECT, income mutation, execution or credential permission.
GRANT SELECT (
    income_id, exchange, account_id, environment, product, symbol,
    income_type, amount, currency, occurred_at_ms, evidence
) ON trade_v2.v2_exchange_income TO tradev2dash;
