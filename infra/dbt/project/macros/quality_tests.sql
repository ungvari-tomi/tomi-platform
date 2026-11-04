{% test unique_business_key(model, columns) %}
SELECT
    {% for column in columns %}{{ adapter.quote(column) }}{% if not loop.last %}, {% endif %}{% endfor %},
    COUNT(*) AS record_count
FROM {{ model }}
GROUP BY {% for column in columns %}{{ adapter.quote(column) }}{% if not loop.last %}, {% endif %}{% endfor %}
HAVING COUNT(*) > 1
{% endtest %}

{% test positive(model, column_name) %}
SELECT *
FROM {{ model }}
WHERE {{ adapter.quote(column_name) }} <= 0
{% endtest %}
