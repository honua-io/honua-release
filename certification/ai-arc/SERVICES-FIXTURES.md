# ARC services fixture interface

The fixture identifier selects the corpus data and its frozen expectations. It does not identify
which task the customer requested. A trusted case-to-scenario assignment must accompany the
in-memory goal; the runner never guesses a task from customer prose. Missing or unknown assignments
and expectations fail closed before a session is started.

| Fixture ID | Corpus variant | Scenario expectations available |
| --- | --- | --- |
| town-A | A | P10, P12, P13 |
| town-B | B | P10, P12, P13 |

P07 has neither a scenario module nor frozen local expectations on this checkout; it is refused.
P12 requires a server restart between sessions, which the current adopted ARC runtime refuses, so
its presence in the corpus catalog does not mean ARC can execute it successfully. The catalog does
not admit other fixture IDs, including fixtures without frozen expectations.

The ARC owner can call `ai_arc_services.run_services` with `goal`, `fixture_id` and the authoritative
`scenario_id`, and `scenarios` containing exactly that ID. `variant` must agree with the catalog.
The goal remains in memory until passed to the agent session; the runner substitutes it for the
pack prompt and scores against the fixture's frozen expectations. The caller must not derive the
scenario from arbitrary goal text, and is responsible for obtaining the assignment from an
integrity-bound catalog. This interface does not install the manifest's fault: fault admission and
injection require separate integration.
