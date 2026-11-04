{{ config(materialized='view') }}
SELECT dataset_id, order_id, created_at, country, currency, product_id,
       quantity, unit_price_minor, total_minor, customer_token
FROM {{ source('customer_demo', 'orders') }}
