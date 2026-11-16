# ADR-001: Pragmatic Clean Architecture modular monolith

- Status: Accepted
- Date: 2026-07-27

## Decision

Build one Python application with domain, application, infrastructure,
presentation and worker boundaries. Domain dependencies point inward; the
application is not split into separately deployed services.

## Consequences

Boundary tests and explicit composition are required. Deployment and local
operation remain simple, while framework code stays outside the domain.
