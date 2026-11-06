# Reference fixture

`weather_agent.json` contains the `expected_agent_json` object from the source
framework's `tests/golden/inputs/seq_Observability_Probes_Scenario_2_agent1_Weather_Reporter_Agent.json`,
at source commit `ba3f182ed23967f248e63b77705c898fdd2fe7eb`.

It exercises legacy interrupted-tool results and workflow step renumbering.
The tests copy and mutate it in temporary directories. It requires no application
code, datasets, provider credentials, or network calls, and stays outside all
plugin distributions.
