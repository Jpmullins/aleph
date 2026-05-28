## Instructions
1. Review the codebase at ~/code/ARLIS/open-analyst
    a. There are UI/UX ideas to borrow
        i. The activity card at the top of the chat that displays planning, subagents, etc.
        ii. The hypothesis tab structure - but ACH matrix, confidence, etc to A2UI genearatable cards.
        iii. The Notes structure - Converted to A2UI cards where user can create notes in markdown that are then rendered and can be published to wiki on request
        iv. The artifacts tab is more what the Aleph artifacts tab should be -- a place to view the source material store in RKS (rendered properly in native formats (pdf, docx, xlsx, etc.)) Converted into a A2UI cards (but artifacts should be searchable, and discoverable, etc).
        v. The aesthetic is much better in open analyst (colors, dark/light mode, other UI best practices)
    b. There are backend ideas to borrow
        i. The agent - subagent structure of Deep Agents
        ii. Async subagents for context management and background tasks.

2. Review docs-langchain mcp and copilotkit-mcp
    a. We need to really update how we are using deep agents and copilotkit
    b. focus on subagents, skills.md use, session persistance, context managment, etc. 
    c. We want to not just use default behavior, but look for ways to bring out full potential
    d. Copilotkit coagents are essentail for UI/assistant interactions and awareness
        i. assistant needs to be able to interact with and see what user is doing in right panel UI cards, etc.--
        ii. how to integrate the three components well (A2UI, copilotkit, and deep agents).
        iii. Bring out full potential of everything

3. Background agents
    a. What agents exist?
    b. How are they used/deployed?
    c. Does it match the specs--how does it differ?
    d. ARe they communicating well (a2a)
    e. Are agents given well-designed subagents to help them with their specific task?

4. Wiki progress
    a. I don't see any way to tell if wiki bootstrap is working or not--we should see some kind of progress graphic and report and signals of how the bootstrap and updates to wiki are going
    b. Maybe in proper activity cards (again see deep agents frontend and copilotkit)

5. We need to fully unlock potential of A2UI
    a. again review ~/code/A2UI
