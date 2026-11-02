select s.dataset_id, s.order_id
from {{ ref('stg_orders') }} s
full outer join {{ ref('orders_eur') }} e using (dataset_id, order_id)
where s.dataset_id is null or e.dataset_id is null
