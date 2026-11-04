{{ config(materialized='view') }}
select dataset_id, customer_token, as_of, country, age_band, synthetic
from {{ source('customer_demo', 'customers') }}
