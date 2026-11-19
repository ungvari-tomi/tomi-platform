{{ config(materialized='table') }}

SELECT
    dataset_id, order_date, country_code, product_id, currency_code,
    COUNT(*) AS order_count,
    SUM(quantity)::BIGINT AS units_sold,
    COUNT(eur_total_minor) AS fx_covered_orders,
    COUNT(*) - COUNT(eur_total_minor) AS fx_uncovered_orders,
    SUM(eur_total_minor)::HUGEINT AS revenue_eur_minor,
    MAX(ordered_at) AS latest_order_at
FROM {{ ref('orders_eur') }}
GROUP BY dataset_id, order_date, country_code, product_id, currency_code
