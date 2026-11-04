{{ config(materialized='view') }}
select dataset_id, order_id, created_at, country, currency, product_id,
       quantity, unit_price_minor, total_minor, customer_token
from {{ source('customer_demo', 'orders') }}
