SELECT s.dataset_id, s.order_id
FROM {{ ref('stg_orders') }} s
FULL OUTER JOIN {{ ref('orders_eur') }} e USING (dataset_id, order_id)
WHERE s.dataset_id IS NULL OR e.dataset_id IS NULL
