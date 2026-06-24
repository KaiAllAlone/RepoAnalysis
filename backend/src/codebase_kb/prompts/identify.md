You are reverse-engineering a codebase into a tutorial. The codebase has been
pre-analyzed: a dependency graph has been built and PageRank + community
detection have been run. Below are the **top {{top_k}} modules by PageRank**,
**pre-grouped into {{community_count}} communities** by Louvain community
detection on the graph.

Symbols and file paths in the input below are **guaranteed to exist** in the
codebase. Reference them by exact name. Do not invent symbols or file paths.

## Communities (each is a likely chapter group)

{% for community in communities %}
### Community {{ loop.index }} (PageRank sum: {{ community.pagerank_sum | round(3) }})
{% for mod in community.top_modules %}
- `{{ mod.path }}` — top functions: {{ mod.top_functions | join(", ") }}{% if mod.top_classes %} — top classes: {{ mod.top_classes | join(", ") }}{% endif %}
{% endfor %}
{% endfor %}

## Top {{ top_k }} Modules by PageRank (flat, ranked)

{% for mod in top_modules %}
{{ loop.index }}. `{{ mod.path }}` (PR={{ mod.pagerank | round(4) }})
   - functions called heavily: {{ mod.in_degree_functions | join(", ") or "(none)" }}
   - classes: {{ mod.classes | join(", ") or "(none)" }}
{% endfor %}

## Your Task

Cluster the modules above into **between 5 and {{ max_abstractions }} core
abstractions** (concepts that a complete newbie onboarding to this repo would need
to understand). An abstraction should be a thematic concept, not a single
file — but it should be anchored to real modules.Make the abstraction very clear to understand and avoid using technical jargon and if any technical keywords must be used clearly explain its meaning also.

Output a single JSON array fenced with ```json. Follow this exact schema:

```json
[
  {
    "name": "Authentication",
    "description": "Handles user login, session creation, and token issuance.",
    "anchor_modules": [
      "src/auth/service.py",
      "src/auth/models.py"
    ]
  }
]
```

Constraints:
- Total count of objects in the array must be between 5 and {{ max_abstractions }}.
- Every string in the `anchor_modules` array must be an exact file path from the input above.
- Prefer community-aligned groupings; resist splitting a community across multiple abstractions unless it spans >10 modules.
- Write descriptions in {{ language }}.

Output ONLY the JSON block. Do not include any conversational text before or after.