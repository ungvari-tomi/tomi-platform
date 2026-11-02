{{ config(materialized='view') }}

select
    cast(dataset_id as varchar) as dataset_id,
    cast(order_id as bigint) as order_id,
    cast(created_at as timestamptz) as ordered_at,
    cast(country as varchar) as country_code,
    cast(currency as varchar) as currency_code,
    cast(product_id as varchar) as product_id,
    cast(quantity as integer) as quantity,
    cast(unit_price_minor as bigint) as unit_price_minor,
    cast(total_minor as bigint) as total_minor,
    cast(first_manifest as varchar) as source_manifest_path
from {{ source('retailflow_raw', 'orders') }}
