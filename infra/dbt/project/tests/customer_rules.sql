{{ config(enabled=var('customer_pipeline', false), tags=['customers']) }}
select 'order' as issue from {{ ref('stg_customer_orders') }}
where not regexp_full_match(customer_token, 'ct1_[a-z0-9-]{1,24}_[a-f0-9]{64}')
   or quantity not between 1 and 5 or unit_price_minor <= 0
   or total_minor != quantity * unit_price_minor
union all
select 'customer' as issue from {{ ref('stg_customers') }}
where not regexp_full_match(customer_token, 'ct1_[a-z0-9-]{1,24}_[a-f0-9]{64}')
   or synthetic is distinct from true or as_of > current_date
