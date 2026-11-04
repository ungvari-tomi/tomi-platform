{{ config(materialized='view') }}
-- Preserve every order. Only join a snapshot delivered with that order in a verified batch.
SELECT o.*, c.as_of AS customer_as_of, c.country AS customer_country,
       c.age_band, c.synthetic
FROM {{ ref('stg_customer_orders') }} o
LEFT JOIN {{ source('customer_demo', 'order_batches') }} ob
  ON ob.dataset_id = o.dataset_id AND ob.order_id = o.order_id
LEFT JOIN {{ source('customer_demo', 'customer_batches') }} cb
  ON cb.manifest = ob.manifest AND cb.dataset_id = o.dataset_id
 AND cb.customer_token = o.customer_token
LEFT JOIN {{ ref('stg_customers') }} c
  ON c.dataset_id = cb.dataset_id AND c.customer_token = cb.customer_token
 AND c.as_of = cb.as_of
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY o.dataset_id, o.order_id
  ORDER BY c.as_of DESC NULLS LAST, ob.manifest DESC
) = 1
