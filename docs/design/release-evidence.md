# Documentation and evidence ownership

Root STATUS is the current source-contract entrypoint; README and USAGE own
operations, VERIFICATION owns test commands, and component design documents own
adapter semantics. Dated plans are historical records. Moving old prose into the
history archive preserves exact bytes and a source/path/SHA-256 manifest instead
of editing past results to resemble the present. Archive-relative links retain
their original meaning at the recorded source revision.

The release-evidence command owns observed local test results. It runs the checked
synthetic suite, records JUnit test-case counts without relabeling subtests as
ordinary test counts, and binds the observation to before/after source identity
and actual Python/OS. Dirty or moving checkouts cannot earn revisionVerified.
Missing/malformed reports and failed steps remain failures even if partial data
exists. Reports are retained on failure so a release author can explain the gap.

Artifact hashes bind optional installed tests to the supplied wheel/sdist bytes.
They do not attest that those bytes were built from the checkout. Build provenance,
live provider verification, publication, hosted CI and native service-manager
execution are explicit non-claims. A local pass cannot be promoted to those claims
by editing a static status table. No new runtime dependency is required.

Documented shell examples are parsed without dispatch using the production
command parsers. Tests cover current README/USAGE/STATUS/VERIFICATION shell blocks;
comments, inline prose, historical records and unrelated shell commands are not
executed or certified. A grammar pass does not demonstrate provider entitlement
or success of a destructive/mutating command.
