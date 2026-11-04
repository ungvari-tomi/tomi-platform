{{ config(materialized='view') }}
SELECT dataset_id, customer_token, as_of, country, age_band, synthetic
FROM {{ source('customer_demo', 'customers') }}
