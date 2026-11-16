---
role: coder
version: "1"
output_schema: null
---
without it, the full run executes on one NVIDIA L40S GPU and must fit the plan's budget: put the model and tensors on `cuda` when `torch.cuda.is_available()`, else on `cpu` (the smoke run is on CPU)
