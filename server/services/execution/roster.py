"""SQLite compatibility adapter for legacy name-based callers."""
from pathlib import Path
from .registry import AgentRegistry, get_registry


class AgentRoster:
    def __init__(self, roster_path: Path, registry: AgentRegistry | None = None):
        self._roster_path, self._registry = roster_path, registry

    @property
    def registry(self) -> AgentRegistry:
        return self._registry or get_registry()

    def load(self) -> None:
        if self._roster_path.is_file():
            self.registry.migrate_legacy(self._roster_path, self._roster_path.parent)

    def save(self) -> None:
        pass

    def add_agent(self, agent_name: str) -> str:
        self.load()
        existing = self.registry.resolve_alias(agent_name)
        if existing:
            return existing
        record = self.registry.create_agent(agent_name, f"Legacy task: {agent_name}")
        self.registry.update_agent(record["id"], legacy_alias=agent_name)
        return record["id"]

    def get_agents(self) -> list[str]:
        return [r["legacy_alias"] or r["id"] for r in self.registry.eligible_agents()]

    def clear(self) -> None:
        if self._roster_path.is_file():
            self._roster_path.unlink()


_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
_agent_roster = AgentRoster(_DATA_DIR / "execution_agents" / "roster.json")


def get_agent_roster() -> AgentRoster:
    return _agent_roster
