{% test unique_business_key(model, columns) %}
select
    {% for column in columns %}{{ adapter.quote(column) }}{% if not loop.last %}, {% endif %}{% endfor %},
    count(*) as record_count
from {{ model }}
group by {% for column in columns %}{{ adapter.quote(column) }}{% if not loop.last %}, {% endif %}{% endfor %}
having count(*) > 1
{% endtest %}

{% test positive(model, column_name) %}
select *
from {{ model }}
where {{ adapter.quote(column_name) }} <= 0
{% endtest %}
