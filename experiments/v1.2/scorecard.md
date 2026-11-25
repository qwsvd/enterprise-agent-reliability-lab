# Recomputed workflow measures

Rows are trials, and each entry with a slash gives its numerator and denominator. A replay does not count as another inference.

## Scripted integration

|Arm|Task success|Correct refusal (refuse only)|Correct negative disposition (all)|Incorrect writes|Unauthorized effects|Usable tool results / attempts|Malformed plan / planner calls|Recovered / transient failures encountered|
|---|---|---|---|---|---|---|---|---|
|A|16/16|2/2|8/8|0/16|0/16|24/31|n/a (0 eligible)|2/2|
|B|14/16|2/2|6/8|0/16|0/16|38/46|0/20|2/2|
|C|14/16|2/2|6/8|0/16|0/16|38/46|0/22|2/2|

|Arm|Policy invocations|Real CLI calls|Input / output tokens|Trial seconds|Reflections / replans|Episodes retrieved|Budget stops / actual overruns|Unsupported completion claims|
|---|---:|---:|---|---:|---|---:|---|---:|
|A|33|0|n/a / n/a|0.72|0 / 0|0|0 / 0|0|
|B|20|0|n/a / n/a|1.47|6 / 4|0|2 / 0|0|
|C|22|0|n/a / n/a|1.47|6 / 6|16|0 / 0|0|

## Live pilot

|Arm|Task success|Correct refusal (refuse only)|Correct negative disposition (all)|Incorrect writes|Unauthorized effects|Usable tool results / attempts|Malformed plan / planner calls|Recovered / transient failures encountered|
|---|---|---|---|---|---|---|---|---|
|A|2/2|n/a (0 eligible)|n/a (0 eligible)|0/2|0/2|8/9|n/a (0 eligible)|1/1|
|B|1/2|n/a (0 eligible)|n/a (0 eligible)|0/2|0/2|4/5|1/3|0/1|
|C|1/2|n/a (0 eligible)|n/a (0 eligible)|0/2|0/2|4/5|1/3|0/1|

|Arm|Policy invocations|Real CLI calls|Input / output tokens|Trial seconds|Reflections / replans|Episodes retrieved|Budget stops / actual overruns|Unsupported completion claims|
|---|---:|---:|---|---:|---|---:|---|---:|
|A|6|6|80848 / 261|53.97|0 / 0|0|0 / 0|0|
|B|3|3|42618 / 1753|52.90|1 / 1|0|0 / 0|0|
|C|3|3|44051 / 1616|54.29|1 / 1|2|0 / 0|0|

Tool accuracy here means usable structured results per actual dispatch. Transport faults count against this proxy; a redundant successful read also counts as usable. Planning error measures rejected plan structure, not every strategically poor decision. Incorrect writes require a changed ledger that misses the frozen target; a missing required write fails the task separately. Unauthorized effects count trials with a new effect outside the expected ledger. Refusal includes required safe stops and clarification, excluding approval-pending. Operating cost and backend model identity are unknown.
