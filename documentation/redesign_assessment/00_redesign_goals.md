# Goals for the redesign

## Testability
- The entire system should be easily testable with automated tests in such a way that allows for usage of the platform with mocks in place of real services.
- We should be able to simulate and test workflows end-to-end in a reliable manner.
- The mocks should be available not just for testing, but also for local development and experimentation.
- Not everything needs to be mocked, aspects like docker are effectivelly already well abstracted and can just be used as is.
- Testing should be able to run end-to-end, exercising key business logic without Github issues or real Claude Code

## Observability
- The system has a robust observability story, with logging, metrics, and tracing in place to allow for easy debugging and monitoring of the system.
- Normalization of the eventing should make it more reliable and more valuable for debugging and monitoring.
- The current observability tech stack is great and should be reused as much as possible.

## Extensibility
- The system should be designed in a way that allows for easy addition of new features and capabilities in the future.
- The system should be defined with modularity in mind, allowing for components to be swapped out or extended as needed.
  - Different ticketing systems (Jira, Github Issues, markdown files in folder, etc.) should be easily pluggable into the system.
  - Different LLM providers should be easily pluggable into the system (Claude Code, Aider, etc.)

## Ease of Configuration
- The current system relies heavily on yaml files for configuration, which can be cumbersome and error-prone.
- The redesigned system should provide a more user-friendly way to configure the system through a web interface and storing the data in a database (we have elasticsearch already set up for this).
- Agent prompts and behaviors should be easily configurable through the web interface as well with clear configuration stored in the database.
- The system should provide sensible defaults for configurations to minimize the amount of setup required for new users