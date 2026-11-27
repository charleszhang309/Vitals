"""In-memory counter demonstrating the Deck plugin hooks."""

_count = 0


def execute_action(action_id, params):
    global _count
    if action_id != "increment":
        raise ValueError("Unknown action")
    _count += params["amount"]
    return {"count": _count}


def get_state(feedback_id, params):
    if feedback_id != "at-target":
        raise ValueError("Unknown feedback")
    return _count >= params["target"]


def get_variables():
    return {"count": _count}
