"""The test author's evolvable layer (spec "出题者进化循环 v2").

The author's prompt is assembled from three parts: the fixed rules
(``configs/roles/test_author/core_rules.md``, never evolved), the rules
document (``configs/author/rules_doc``, the evolution object) and, when the
inventory feature is on, the milestone's documented-behaviour inventory.
Everything here is off unless the run sets ``ADAMAS_AUTHOR_RULES_DOC``; with it
unset the author reads exactly the ``test_author.yaml`` prompt it always did.
"""
