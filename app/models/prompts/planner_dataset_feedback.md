---
role: planner
version: "1"
output_schema: app.domain.models.Plan
---
Code checked the datasets of your plan against Hugging Face. Jobs fetch only public, ungated
datasets with parquet files for the split, without any token. These cannot be fetched:
{{problems}}

Return the whole plan again. Replace each of them with a Hugging Face dataset id that exists and
has parquet files for the split (a dataset listed in the discovery when there is one), or drop it
and say so in notes. Never invent a dataset id. Keep the rest of the plan unless the change
requires it.
