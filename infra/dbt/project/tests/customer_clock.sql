{{ config(enabled=var('customer_pipeline', false), tags=['customers', 'customer_freshness']) }}
select 'future_order' as issue from {{ source('customer_demo', 'orders') }}
where created_at > current_timestamp
union all
select 'future_load' as issue from {{ source('customer_demo', 'batches') }}
where loaded_at > current_timestamp
