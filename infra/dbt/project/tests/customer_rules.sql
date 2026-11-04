{{ config(enabled=var('customer_pipeline', false), tags=['customers']) }}
SELECT 'order' AS issue FROM {{ ref('stg_customer_orders') }}
WHERE NOT REGEXP_FULL_MATCH(customer_token, 'ct1_[a-z0-9-]{1,24}_[a-f0-9]{64}')
   OR quantity NOT BETWEEN 1 AND 5 OR unit_price_minor <= 0
   OR total_minor != quantity * unit_price_minor
UNION ALL
SELECT 'customer' AS issue FROM {{ ref('stg_customers') }}
WHERE NOT REGEXP_FULL_MATCH(customer_token, 'ct1_[a-z0-9-]{1,24}_[a-f0-9]{64}')
   OR synthetic IS DISTINCT FROM TRUE OR as_of > CURRENT_DATE
