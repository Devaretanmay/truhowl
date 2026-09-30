TASK_PROFILES = {
    "code": ["upgrade", "update", "bump", "add", "install", "generate", "create", "write", "refactor", "restructure"],
    "debugging": ["fix", "bug", "error", "crash", "broken", "issue"],
    "research": ["explore", "investigate", "understand", "what", "how", "why", "list", "show", "find", "search"],
    "testing": ["test"],
    "security": ["security", "auth", "permission", "vulnerability", "injection"],
    "writing": ["document", "readme", "comment", "docstring", "docs"],
}


def classify(text: str) -> str:
    words = text.strip().lower().split()
    first_word = words[0] if words else ""
    for profile, keywords in TASK_PROFILES.items():
        if first_word in keywords or any(w in keywords for w in words[:3]):
            return profile
    return "code"
