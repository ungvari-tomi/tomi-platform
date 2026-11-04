{{ config(enabled=var('customer_pipeline', false), tags=['customers']) }}
-- Missing, orphaned or inconsistent snapshots remain visible as failing checks.
select 'missing_or_mismatched' as issue
from {{ ref('customer_orders') }}
where customer_as_of is null or customer_country != country
   or synthetic is distinct from true
union all
select 'orphan_snapshot' as issue
from {{ ref('stg_customers') }} c
where not exists (
  select 1 from {{ source('customer_demo', 'customer_batches') }} cb
  join {{ source('customer_demo', 'order_batches') }} ob
    on ob.manifest = cb.manifest and ob.dataset_id = cb.dataset_id
  join {{ ref('stg_customer_orders') }} o
    on o.dataset_id = ob.dataset_id and o.order_id = ob.order_id
   and o.customer_token = cb.customer_token
  where cb.dataset_id = c.dataset_id and cb.customer_token = c.customer_token
    and cb.as_of = c.as_of
)
union all
select 'order_count_changed' as issue
where (select count(*) from {{ ref('stg_customer_orders') }})
   != (select count(*) from {{ ref('customer_orders') }})
