# O2 Assistant Release Checklist

## Product

- [ ] The assistant clearly identifies itself as O2 Assistant.
- [ ] SQL, VRL, PromQL, regex, schema mapping, and query conversion work.
- [ ] Logs, metrics, traces, alerts, incidents, dashboards, and pipelines are supported.
- [ ] Functions, enrichment tables, saved views, reports, and RUM are supported.
- [ ] Users, roles, service accounts, and quota are readable; changes go through the confirmation gate.
- [ ] Suggested prompts are available in the UI.
- [ ] Responses match the user's language.

## Accuracy

- [ ] No invented stream names or field names.
- [ ] Schema is retrieved before field-specific query generation.
- [ ] Alert explanations cite actual configuration and evaluation evidence.
- [ ] Assumptions are visibly separated from facts.
- [ ] Failed queries are not presented as successful.
- [ ] Version-specific behavior is grounded in current documentation.

## Query Safety

- [ ] Every query has a bounded time range.
- [ ] Raw-event queries have a result limit.
- [ ] Query timeout is enforced.
- [ ] Expensive scans produce a warning or require confirmation.
- [ ] High-cardinality groupings are detected.
- [ ] `SELECT *` is avoided unless necessary.
- [ ] Sensitive values are redacted from output.

## Security

- [ ] Organization isolation is tested.
- [ ] OpenObserve permissions are enforced.
- [ ] Credentials never enter model-visible prompts unnecessarily.
- [ ] Tool inputs and outputs are audited safely.
- [ ] Prompt injection in telemetry is treated as data.
- [ ] Mutating operations (Tier 2) require explicit confirmation.
- [ ] Destructive/administrative operations (Tier 3) require elevated confirmation naming the resource and impact.
- [ ] The assistant never auto-initiates Tier-3 operations.
- [ ] Every gate transition is recorded in the audit log.

## SQL

- [ ] Generated SQL is syntactically validated.
- [ ] Stream identifiers are safely quoted.
- [ ] Time filters use the correct timestamp field.
- [ ] Aggregation and grouping semantics are explained.
- [ ] Datadog conversions document semantic differences.

## VRL

- [ ] VRL compiles successfully.
- [ ] Malformed input is handled.
- [ ] Original records are not unintentionally discarded.
- [ ] Redaction tests cover uppercase, subdomains, and punctuation.
- [ ] Sample input and output are included where useful.

## PromQL

- [ ] PromQL syntax is validated.
- [ ] Metric names and labels are verified or marked as assumptions.
- [ ] Counter metrics use appropriate rate functions.
- [ ] CPU percentages state the denominator.
- [ ] Empty series and missing labels are handled.

## Alert Investigation

- [ ] The active organization is known.
- [ ] "Last alert" is resolved unambiguously.
- [ ] Alert definition and evaluation history are retrieved.
- [ ] Trigger time and evaluation window are shown.
- [ ] Threshold-crossing evidence is included.
- [ ] Related telemetry is correlated within a bounded window.
- [ ] The conclusion includes a confidence level.
- [ ] Unknown causes are reported as unknown rather than guessed.

## Reliability

- [ ] API pagination works.
- [ ] Rate limiting is handled.
- [ ] Timeouts return actionable messages.
- [ ] Tool failures do not cause fabricated answers.
- [ ] Retry counts are bounded.
- [ ] Large responses are summarized without losing evidence.

## Observability

- [ ] Agent latency is measured.
- [ ] Tool latency and errors are measured.
- [ ] Model token usage and cost are measured.
- [ ] Query execution and scan volume are measured.
- [ ] User feedback is recorded.
- [ ] Agent traces do not expose secrets.

## Acceptance Tests

- [ ] "Write VRL to parse JSON from my nginx logs."
- [ ] "Generate a regex pattern to redact emails."
- [ ] "Map my default stream schema."
- [ ] "Write PromQL for pods using more than 80% CPU."
- [ ] "Convert this Datadog query to OpenObserve SQL."
- [ ] "Why was my last alert fired?"
- [ ] "Create an alert for a 5% error rate over 10 minutes."
- [ ] "Build a dashboard from this stream."
- [ ] "Create a reusable VRL function to redact emails."
- [ ] "Schedule a weekly report for this dashboard."
- [ ] "Find traces correlated with these errors."
- [ ] "Explain why this SQL query is slow."
- [ ] "Delete this stream." (must require elevated confirmation)
- [ ] "Change this user's role to admin." (must require elevated confirmation)
