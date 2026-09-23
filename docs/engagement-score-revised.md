### Engagement Score

Calculate an Instagram engagement-potential score from the article **title + summary only**.

**Do not score:** headline quality, source quality, writing quality, city relevance, or carousel significance.

#### Dimensions & weights

| Dimension            | Weight | What it measures                                                          |
| -------------------- | -----: | ------------------------------------------------------------------------- |
| `emotional_salience` |    20% | Emotional reaction: surprise, concern, anger, excitement, sadness         |
| `audience_breadth`   |    20% | How broadly the story is relevant to the target city's population         |
| `impact`             |    20% | Real-world consequences for people, businesses, infrastructure, or safety |
| `novelty`            |    15% | How unusual or noteworthy the event is                                    |
| `human_interest`     |    10% | How strongly the story centers on people or relatable experiences         |
| `timeliness`         |    10% | How much value depends on seeing it now/soon                              |
| `visual_potential`   |     5% | How naturally the event lends itself to compelling visuals                |

### Enum values

Use the same four values for every dimension:

* `VERY_LOW` = minimal
* `LOW` = limited
* `HIGH` = substantial
* `VERY_HIGH` = exceptional

Interpret them **relative to that specific dimension**.

Examples:

* Routine municipal meeting → low emotional salience, low novelty
* Neighborhood road closure → low audience breadth but potentially high impact
* City-wide water outage → very high audience breadth and impact
* Major fire / building collapse → very high emotional salience and visual potential
* Rare animal appearing in a residential area → very high novelty and visual potential
* Routine administrative announcement → very low visual potential

**Important:** Score each dimension independently. Do not let one dimension automatically increase another.

### LLM output

Return only structured JSON:

```json
{
  "emotional_salience": "HIGH",
  "audience_breadth": "VERY_HIGH",
  "impact": "HIGH",
  "novelty": "VERY_HIGH",
  "human_interest": "HIGH",
  "timeliness": "HIGH",
  "visual_potential": "HIGH"
}
```

Do not return numeric scores.

### Backend scoring

Map:

```python
VERY_LOW = 0.00
LOW = 0.33
HIGH = 0.67
VERY_HIGH = 1.00
```

Then:

```python
score = 10 * (
    0.20 * emotional_salience
    + 0.20 * audience_breadth
    + 0.20 * impact
    + 0.15 * novelty
    + 0.10 * human_interest
    + 0.10 * timeliness
    + 0.05 * visual_potential
)
```

Round to **1 decimal place**.

Only use information supported by the title/summary. Do not speculate about consequences or context that is not provided.
