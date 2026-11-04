{{ config(materialized='view') }}
-- Preserve every order. Only join a snapshot delivered with that order in a verified batch.
select o.*, c.as_of as customer_as_of, c.country as customer_country,
       c.age_band, c.synthetic
from {{ ref('stg_customer_orders') }} o
left join {{ source('customer_demo', 'order_batches') }} ob
  on ob.dataset_id = o.dataset_id and ob.order_id = o.order_id
left join {{ source('customer_demo', 'customer_batches') }} cb
  on cb.manifest = ob.manifest and cb.dataset_id = o.dataset_id
 and cb.customer_token = o.customer_token
left join {{ ref('stg_customers') }} c
  on c.dataset_id = cb.dataset_id and c.customer_token = cb.customer_token
 and c.as_of = cb.as_of
qualify row_number() over (
  partition by o.dataset_id, o.order_id
  order by c.as_of desc nulls last, ob.manifest desc
) = 1
