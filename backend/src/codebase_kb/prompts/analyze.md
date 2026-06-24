You are analyzing the architectural relationships between the core abstractions of a codebase.
Below are the identified **abstractions** and the **underlying code-level edges** between them.

## Identified Abstractions

{% for abs in abstractions %}
- **{{ abs.name }}**: {{ abs.description }}
{% endfor %}

## Code-Level Edges (Raw Dependencies)
These edges are aggregated from actual imports, function calls, and subclassing relationships in the code.
{% for edge in edges %}
- `{{ edge.from_name }}` uses `{{ edge.to_name }}` ({{ edge.kind }}, {{ edge.src_count }} references)
{% endfor %}

## Your Task

Analyze the provided abstractions and their raw code-level dependencies to describe the high-level architecture.

1. **Synthesize Relationships**: Convert the raw dependency edges into meaningful architectural relationships. Give each relationship a short semantic label (e.g., "authenticates users with", "persists data using").
2. **Architecture Summary**: Write a short paragraph summarizing how these abstractions fit together to form the overall system.

Output a single JSON object fenced with ```json. Follow this exact schema:

```json
{
  "relationships": [
    {
      "from": "Authentication",
      "to": "Database Models",
      "label": "verifies credentials against",
      "kind": "semantic"
    }
  ],
  "summary": "The system uses a layered architecture where Authentication acts as a middleware..."
}
```

Constraints:
- Only include relationships where the `from` and `to` are EXACT names from the "Identified Abstractions" list.
- Do NOT output self-relationships (where `from` equals `to`).
- Keep the `label` concise (under 60 characters).
- Write the summary and labels in {{ language }}.
- Output ONLY the JSON block. Do not include conversational text before or after.
