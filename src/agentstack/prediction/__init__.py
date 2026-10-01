"""Layer 4 - tabular inference. Not the language model (Model engine & inference).

`agentstack.model` is the language model: an interaction contract, prompts, tool
proposals. This package is TabPFN, which predicts churn from a table and has no opinion
about text. They are both "models" in English and nothing alike in what they need -
different assets, different serving, different failure modes, different licences. One
package called `model` holding both would make "the model version" mean two things in
an experiment's provenance, which is the Model engine & inference confusion in miniature.

The deliberate consequence: nothing in this package decides *who* gets an offer. It
returns a probability per row. The targeting predicate is policy and lives elsewhere,
because a scorer that also chose the cohort would be a model making an authority
decision.
"""
