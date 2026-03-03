# Code Conventions

- Before adding retry/error-handling logic, grep for existing patterns in the codebase (e.g. tenacity in tool_sampler.py) and match the established style
- When adding a library or pattern, grep for existing usage first and follow the same approach rather than writing from scratch
- When passing a single boolean `True`/`False` as an argument, always use the keyword form (e.g. `get_timezone(complex_chat=True)` not `get_timezone(True)`)
- Always use top-level imports, never local/deferred imports unless needed to break a circular dependency
- All imports must be at the top of the file, never scattered between function definitions
