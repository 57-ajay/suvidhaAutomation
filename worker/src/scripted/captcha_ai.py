# worker/src/scripted/captcha_ai.py
"""AI captcha solve — used ONLY by the pending-transaction clear flow.

This is a deliberate asymmetry from the main captcha: the pending-clear page is a
background recovery the driver shouldn't have to babysit, so we let a scoped AI
agent read the canvas and type it. Returns (text, cost_usd).

Cost is captured so it flows into agentCost.totalCost. Typical cost is a couple
of cents per solve.
"""

from __future__ import annotations

from .llm import build_llm


async def ai_read_captcha(session) -> tuple[str, float]:
    """Read the captcha on the current page using a scoped browser-use Agent.

    Returns the characters (or 'UNREADABLE') and the USD cost of the call.
    """
    from browser_use import Agent, Tools

    captured = {"text": ""}
    tools = Tools()

    @tools.action(
        description=(
            "Submit the captcha characters you read. Pass them as one string, no "
            "spaces. If unreadable, pass 'UNREADABLE'."
        )
    )
    async def submit_captcha(text: str) -> str:  # noqa: ANN001
        captured["text"] = (text or "").strip()
        return "ok"

    prompt = (
        "Look ONLY at the captcha image on the current page (a small canvas with "
        "distorted characters near a 'Go' button). Read the characters and call "
        "submit_captcha with exactly those characters, no spaces. Then call done. "
        "Do NOT click or type anything else."
    )

    agent = Agent(task=prompt, llm=build_llm(), browser=session, tools=tools,
                  calculate_cost=True)
    await agent.run(max_steps=3)

    cost_usd = 0.0
    try:
        usage = await agent.token_cost_service.get_usage_summary()
        cost_usd = float(getattr(usage, "total_cost", 0.0) or 0.0)
    except Exception:
        pass

    return (captured["text"] or "UNREADABLE"), cost_usd
