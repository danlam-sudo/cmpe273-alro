from collections import defaultdict

_sessions: dict[str, list[dict]] = defaultdict(list)


def get_history(session_id: str) -> list[dict]:
    return _sessions[session_id]


def append(session_id: str, role: str, content):
    _sessions[session_id].append({"role": role, "content": content})


def clear(session_id: str):
    _sessions[session_id] = []
