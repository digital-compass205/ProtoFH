I want to build a prototype of a Market Data Feed Handler to check if using AI coding agent could help speeding up the development. 
As a starting tasks, I want to target the Japannext exchange; specs are at https://www.japannext.co.jp/en/support. 
This gives us everything needed for a feed handler prototype I think. They even have sample data.

I want to start with you putting together a full plan for that task, but ensuring it can be break down into small self contained tasks that can be done by a Sonnet or Opus model. So make sure to give precise direction / goals.
We need to have a good way to see the market data moving - CLI output is fine however, no need for something fancy. The static data part should land in a well defined table.

In term of language, python is preferred but if some part are really better in C++ that's fine as well.
Python version is pined to 3.6.4 (the version in RedHat 8.10, which is the target system)

An important part is the actual exchange connectivity. You need to ensure that this is done properly, and assist the user .
You will not have access to a real feed to confirm, so this needs to be built from spec.
That task needs to be early in the plan, to allow checking quickly that we are on the right track, and possibly gather more sample data.

Any question or unclear part please ask. 
Do not start any coding task, just focus on the plan, and making clear and actionable steps.

  > Model: Researched Japannext specs, drafted and saved a TCP-only prototype plan. Refer to JNX_PLAN.md.

Please go forward with first task. Going forward, please commit at key point in a git repositery

  > Model: Completed scaffolding and codec implementation (Phases 0-2) across multiple agent rounds.

Please go forward with Phase 3, but please use a single Sonnet agent to limit token consumption

  > Model: Implemented selectors reactor and core OrderBook logic with full-sample validation.

OK move on to Phase 5

  > Model: Completed snapshot synchronization, exchange simulator, and integration tests.

OK do Phase 7

  > Model: Delivered CLI views, benchmarks (~379k msgs/s), and wrapped up the project.