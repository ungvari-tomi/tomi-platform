-- Compare facts by their keys, preserving missing products and exact amounts.
SELECT o.dataset_id, o.order_id
FROM {{ source('retailflow_raw', 'orders') }} AS o
LEFT JOIN {{ ref('catalog_orders') }} AS e
    ON o.dataset_id = e.dataset_id AND o.order_id = e.order_id
GROUP BY o.dataset_id, o.order_id, o.quantity, o.unit_price_minor, o.total_minor
HAVING COUNT(e.order_id) <> 1 OR MIN(e.quantity) IS DISTINCT FROM o.quantity
    OR MIN(e.unit_price_minor) IS DISTINCT FROM o.unit_price_minor
    OR MIN(e.total_minor) IS DISTINCT FROM o.total_minor
UNION ALL
SELECT i.event_id AS dataset_id, CAST(NULL AS BIGINT) AS order_id
FROM {{ source('inventory_stream', 'inventory_movements') }} AS i
LEFT JOIN {{ ref('catalog_inventory') }} AS e ON i.event_id = e.event_id
GROUP BY i.event_id, i.quantity_delta
HAVING COUNT(e.event_id) <> 1 OR MIN(e.quantity_delta) IS DISTINCT FROM i.quantity_delta
