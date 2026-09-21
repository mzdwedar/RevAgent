---
description: Task breakdown where every task names its layer and its proving test
---

Invoke the `planning-and-task-breakdown` skill. Read `SPEC.md`, `STACK.md` and
`CONSTRAINTS.md` first.

Two additional requirements on every task in the plan:

1. **It names the layer(s) it touches.** A task that touches three layers is usually
   three tasks. A task that cannot name its layer is not understood yet.

2. **It names the test that will prove it.** Either an existing fitness test in
   `tests/fitness/` that must still pass, or a new one to be written. A task with no
   verifying test is not a task — it is a hope.

Order by dependency, slice vertically (one complete path per task, never a horizontal
layer at a time), and put a checkpoint wherever a new side effect, a new capability,
or a new approval boundary is introduced — those are the places where a wrong turn is
expensive to undo.

Save the plan to `tasks/plan.md` and the task list to `tasks/todo.md`, then present it
for review.
